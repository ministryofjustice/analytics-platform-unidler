import contextlib
from http import HTTPStatus
from http.server import HTTPServer, BaseHTTPRequestHandler
import logging
import os
import socket
from socketserver import ThreadingMixIn
import ssl
import sys

import kubernetes
from kubernetes import client, config
from kubernetes.client.models import (
    V1beta1HTTPIngressPath,
    V1beta1HTTPIngressRuleValue,
    V1beta1IngressBackend,
    V1beta1IngressRule,
)


IDLED = 'mojanalytics.xyz/idled'
IDLED_AT = 'mojanalytics.xyz/idled-at'
INGRESS_CLASS = 'kubernetes.io/ingress.class'
UNIDLER = 'unidler'
UNIDLER_NAMESPACE = 'default'

logging.basicConfig(level=os.environ.get('LOG_LEVEL', 'DEBUG'))
log = logging.getLogger('unidler')
logging.getLogger('kubernetes').setLevel(logging.WARNING)


def run(host='0.0.0.0', port=8080):
    try:
        config.load_incluster_config()
    except:
        config.load_kube_config()

    unidler = UnidlerServer((host, port), RequestHandler)
    log.info(f'Unidler listening on {host}:{port}')
    unidler.serve_forever()


class UnidlerServer(ThreadingMixIn, HTTPServer):
    pass


class RequestHandler(BaseHTTPRequestHandler):
    unidling = {}

    def do_GET(self):
        hostname = self.headers.get('X-Forwarded-Host', UNIDLER)

        if hostname.startswith(UNIDLER):
            log.debug('No hostname specified')
            self.respond(HTTPStatus.NO_CONTENT, '')
            return

        try:
            if hostname in self.unidling:
                if self.unidling[hostname].is_done():
                    self.unidling[hostname].enable_ingress()

            else:
                self.unidling[hostname] = Unidling(hostname)
                self.unidling[hostname].start()

        except (DeploymentNotFound, IngressNotFound) as not_found:
            self.send_error(HTTPStatus.NOT_FOUND, str(not_found))

        except Exception as error:
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(error))

        else:
            self.respond(HTTPStatus.ACCEPTED, please_wait(hostname))

    def respond(self, status, body):
        self.send_response(status)
        self.send_header('Content-type', 'text/html')
        self.end_headers()
        self.wfile.write(str(body).encode('utf-8'))


class DeploymentNotFound(Exception):
    pass


class IngressNotFound(Exception):
    pass


class Unidling(object):

    def __init__(self, hostname):
        self.hostname = hostname
        self.ingress = None
        self.deployment = None
        self.started = False
        self.replicas = 0

    def start(self):
        if not self.started:
            self.started = True
            self.ingress = ingress_for_host(self.hostname)
            self.deployment = deployment_for_ingress(self.ingress)

            restore_replicas(self.deployment)
            unmark_idled(self.deployment)

            # XXX writing changes triggers the asynchronous creation of
            # pods, which can take a few seconds
            write_deployment_changes(self.deployment)

    def is_done(self):
        if self.started:
            self.deployment = deployment_for_ingress(self.ingress)
            return (
                IDLED not in self.deployment.metadata.labels and
                self.deployment.status.available_replicas >= 1)
        return False

    def enable_ingress(self):
        ingress = unidler_ingress()
        remove_host_rule(self.hostname, ingress)
        write_ingress_changes(ingress)
        # XXX do we need to wait here for the ingress controller to pick up the
        # changes?
        enable_ingress(self.ingress)
        write_ingress_changes(self.ingress)


def deployment_for_ingress(ingress):
    try:
        return client.AppsV1beta1Api().read_namespaced_deployment(
            ingress.metadata.name,
            ingress.metadata.namespace)

    except kubernetes.client.rest.ApiException as error:
        raise DeploymentNotFound(
            ingress.metadata.name,
            ingress.metadata.namespace)


def ingress_for_host(hostname):
    # XXX assumes first ingress rule is the one we want
    ingresses = client.ExtensionsV1beta1Api().list_ingress_for_all_namespaces()
    ingress = next(
        (
            ingress
            for ingress in ingresses.items
            if (ingress.metadata.name != UNIDLER and
                ingress.spec.rules[0].host == hostname)
        ),
        None)

    if ingress is None:
        raise IngressNotFound(hostname)

    return ingress


def restore_replicas(deployment):
    annotation = deployment.metadata.annotations.get(IDLED_AT)

    if annotation is not None:
        idled_at, replicas = annotation.split(',')
        log.debug(f'Restoring {replicas} replicas')
        deployment.spec.replicas = int(replicas)

    else:
        # TODO Assume a default of 1?
        log.error('Deployment has no idled-at annotation')


def unmark_idled(deployment):
    log.debug('Removing idled annotation and label')
    if IDLED in deployment.metadata.labels:
        del deployment.metadata.labels[IDLED]
    if IDLED_AT in deployment.metadata.annotations:
        del deployment.metadata.annotations[IDLED_AT]


def write_deployment_changes(deployment):
    log.debug(
        f'Writing changes to deployment {deployment.metadata.name} '
        f'in namespace {deployment.metadata.namespace}')
    client.AppsV1beta1Api().replace_namespaced_deployment(
        deployment.metadata.name,
        deployment.metadata.namespace,
        deployment)


def write_ingress_changes(ingress):
    log.debug(
        f'Writing changes to ingress {ingress.metadata.name} '
        f'in namespace {ingress.metadata.namespace}')
    client.ExtensionsV1beta1Api().patch_namespaced_ingress(
        ingress.metadata.name,
        ingress.metadata.namespace,
        ingress)


def unidler_ingress():
    return client.ExtensionsV1beta1Api().read_namespaced_ingress(
        UNIDLER, UNIDLER_NAMESPACE)


def remove_host_rule(hostname, ingress):
    log.debug(
        f'Removing host rules for {hostname} '
        f'from ingress {ingress.metadata.name} '
        f'in namespace {ingress.metadata.namespace}')
    ingress.spec.rules = list(
        filter(
            lambda rule: rule.host != hostname,
            ingress.spec.rules))


def enable_ingress(ingress):
    ingress.metadata.annotations[INGRESS_CLASS] = 'nginx'


def please_wait(hostname):
    with open('please_wait.html') as f:
        body = f.read()
        return body.replace(
            f"UNIDLER_REDIRECT_URL = ''",
            f"UNIDLER_REDIRECT_URL = 'https://{hostname}'")


if __name__ == '__main__':
    run(*sys.argv[1:])
