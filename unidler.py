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
INGRESS_CLASS_NAME = os.environ.get('INGRESS_CLASS_NAME', 'istio')
UNIDLER = 'unidler'
UNIDLER_NAMESPACE = 'default'

logging.basicConfig(level=os.environ.get('LOG_LEVEL', 'DEBUG'))
app_log = logging.getLogger('unidler')
logging.getLogger('kubernetes').setLevel(logging.WARNING)


def run(host='0.0.0.0', port=8080):
    try:
        config.load_incluster_config()
    except:
        config.load_kube_config()

    unidler = UnidlerServer((host, int(port)), RequestHandler)
    app_log.info(f'Unidler listening on {host}:{port}')
    unidler.serve_forever()


class UnidlerServer(ThreadingMixIn, HTTPServer):
    pass


class RequestHandler(BaseHTTPRequestHandler):
    unidling = {}

    def do_GET(self):
        hostname = self.headers.get('Host', UNIDLER)
        if hostname.startswith(UNIDLER):
            app_log.debug('No hostname specified')
            self.respond(HTTPStatus.NO_CONTENT, '')
            return

        username = hostname.split('.')[0]
        log = logging.getLogger('unidler:{}'.format(username))

        try:
            if hostname in self.unidling:
                log.debug('Internal state: unidling is in progress')
                if self.unidling[hostname].is_done():
                    self.unidling[hostname].enable_ingress()
                    del self.unidling[hostname]
                else:
                    log.debug('Unidling is not done yet')

            elif is_idle(hostname):
                log.debug('It is idle, so starting unidling')
                self.unidling[hostname] = Unidling(hostname, log)
                self.unidling[hostname].start()

            else:
                log.error('BAD STATE 4')

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

    def __init__(self, hostname, log=app_log):
        self.hostname = hostname
        self.ingress = None
        self.deployment = None
        self.started = False
        self.replicas = 0
        self.enabled = False
        self.log = log

    def start(self):
        if not self.started:
            self.log.debug('Starting unidle')
            self.started = True
            self.ingress = ingress_for_host(self.hostname)
            self.deployment = deployment_for_ingress(self.ingress)

            restore_replicas(self.deployment, self.log)
            unmark_idled(self.deployment, self.log)

            # XXX writing changes triggers the asynchronous creation of
            # pods, which can take a few seconds
            write_deployment_changes(self.deployment, self.log)
        else:
            self.log.debug('BAD STATE 1')

    def is_done(self):
        if self.started:
            self.deployment = deployment_for_ingress(self.ingress)
            replicas = int(self.deployment.status.available_replicas or 0)
            self.log.debug('Is done?\n  "idled" label removed = {}\n  replicas = {}'.format(
                IDLED not in self.deployment.metadata.labels, replicas))
            return (
                IDLED not in self.deployment.metadata.labels and
                replicas >= 1)
        else:
            self.log.debug('BAD STATE 2')
        return False

    def enable_ingress(self):
        if not self.enabled:
            self.enabled = True
            self.log.debug('Enabling ingress')
            ingress = unidler_ingress()
            remove_host_rule(self.hostname, ingress, self.log)
            write_ingress_changes(ingress, self.log)
            # XXX do we need to wait here for the ingress controller to pick up the
            # changes?
            ingress = ingress_for_host(self.hostname)
            enable_ingress(self.ingress)
            write_ingress_changes(self.ingress, self.log)
        else:
            self.log.debug('BAD STATE 3')


def deployment_for_ingress(ingress):
    try:
        return client.AppsV1beta1Api().read_namespaced_deployment(
            ingress.metadata.name,
            ingress.metadata.namespace)

    except kubernetes.client.rest.ApiException:
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


def is_idle(hostname, log=app_log):
    deployment = deployment_for_ingress(ingress_for_host(hostname))
    log.debug('Is idle?  "idled" label = {}'.format(IDLED in deployment.metadata.labels))
    return IDLED in deployment.metadata.labels


def restore_replicas(deployment, log=app_log):
    annotation = deployment.metadata.annotations.get(IDLED_AT)

    if annotation is not None:
        idled_at, replicas = annotation.split(',')
        log.debug(f'Restoring {replicas} replicas')
        deployment.spec.replicas = int(replicas)

    else:
        # TODO Assume a default of 1?
        log.error('Deployment has no idled-at annotation')

def unmark_idled(deployment, log=app_log):
    log.debug('Removing idled annotation and label')
    if IDLED in deployment.metadata.labels:
        del deployment.metadata.labels[IDLED]
    if IDLED_AT in deployment.metadata.annotations:
        del deployment.metadata.annotations[IDLED_AT]


def write_deployment_changes(deployment, log=app_log):
    log.debug(
        f'Writing changes to deployment {deployment.metadata.name} '
        f'in namespace {deployment.metadata.namespace}')
    client.AppsV1beta1Api().replace_namespaced_deployment(
        deployment.metadata.name,
        deployment.metadata.namespace,
        deployment)


def write_ingress_changes(ingress, log=app_log):
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


def remove_host_rule(hostname, ingress, log=app_log):
    log.debug(
        f'Removing host rules for {hostname} '
        f'from ingress {ingress.metadata.name} '
        f'in namespace {ingress.metadata.namespace}')

    num_rules_before = len(ingress.spec.rules)
    ingress.spec.rules = list(
        filter(
            lambda rule: rule.host != hostname,
            ingress.spec.rules))
    log.debug('Rules removed: {}'.format(num_rules_before - len(ingress.spec.rules)))


def enable_ingress(ingress):
    ingress.metadata.annotations[INGRESS_CLASS] = INGRESS_CLASS_NAME


def please_wait(hostname):
    with open('please_wait.html') as f:
        body = f.read()
        return body.replace(
            f"UNIDLER_REDIRECT_URL = ''",
            f"UNIDLER_REDIRECT_URL = 'https://{hostname}'")


if __name__ == '__main__':
    run(*sys.argv[1:])
