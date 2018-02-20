import contextlib
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


logging.basicConfig(level=os.environ.get('LOG_LEVEL', 'DEBUG'))
log = logging.getLogger('unidler')


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

    def do_GET(self):
        hostname = self.headers.get('X-Forwarded-Host')

        if not hostname:
            return self.respond(200, 'OK')

        log.info(f'Received {self.requestline} for host {hostname}')

        try:
            unidle_deployment(hostname)
            log.info('Unidled {hostname}')
            self.respond(503, 'Unidling, please try again', {
                'Retry-After': 10})

        except (DeploymentNotFound, IngressNotFound) as not_found:
            log.error(not_found)
            self.respond(404, not_found)

        except kubernetes.client.rest.ApiException as error:
            log.error(error)
            self.respond(500, error)

    def do_HEAD(self):
        self.respond(200)

    def do_POST(self):
        self.do_GET()

    def respond(self, status, body=None, headers={}):
        self.send_response(status)
        for header, value in headers.items():
            self.send_header(header, value)
        if 'Content-type' not in headers:
            self.send_header('Content-type', 'text/plain')
        self.end_headers()
        if body:
            self.wfile.write(str(body).encode('utf-8'))


class DeploymentNotFound(Exception):
    pass


class IngressNotFound(Exception):
    pass


def unidle_deployment(hostname):
    ingresses = build_ingress_lookup()

    with ingress_for_host(hostname, ingresses) as ing:
        with deployment_for_ingress(ing) as deployment:
            restore_replicas(deployment)
            unmark_idled(deployment)
            enable_ingress(ing)

    with ingress(UNIDLER, 'default', ingresses) as unidler_ingress:
        remove_host_rule(hostname, unidler_ingress)


def build_ingress_lookup():
    ingresses = client.ExtensionsV1beta1Api().list_ingress_for_all_namespaces()
    return dict(
        ((ingress.metadata.name, ingress.metadata.namespace), ingress)
        for ingress in ingresses.items)


@contextlib.contextmanager
def ingress_for_host(hostname, ingresses):
    for ingress in ingresses.values():
        name = ingress.metadata.name
        namespace = ingress.metadata.namespace

        if (name, namespace) == (UNIDLER, 'default'):
            continue

        for rule in ingress.spec.rules:
            if rule.host == hostname:
                log.debug(
                    f'Found ingress for {hostname}: {name} '
                    f'in namespace {namespace}')
                yield ingress
                return write_ingress_changes(ingress)

    raise IngressNotFound(f'Ingress for host {hostname} not found')


def write_ingress_changes(ingress):
    log.debug(
        f'Writing changes to ingress {ingress.metadata.name} '
        f'in namespace {ingress.metadata.namespace}')
    client.ExtensionsV1beta1Api().patch_namespaced_ingress(
        ingress.metadata.name,
        ingress.metadata.namespace,
        ingress)


@contextlib.contextmanager
def deployment_for_ingress(ingress):
    name = ingress.metadata.name
    namespace = ingress.metadata.namespace

    try:
        deployment = client.AppsV1beta1Api().read_namespaced_deployment(
            name,
            namespace)
        log.debug(f'Found deployment {name} in namespace {namespace}')

        yield deployment

        write_changes(deployment)

    except kubernetes.client.rest.ApiException as error:
        raise DeploymentNotFound(f'Deployment {name} not found in {namespace}')


def write_changes(deployment):
    log.debug(
        f'Writing changes to deployment {deployment.metadata.name} '
        f'in namespace {deployment.metadata.namespace}')
    client.AppsV1beta1Api().patch_namespaced_deployment(
        deployment.metadata.name,
        deployment.metadata.namespace,
        deployment)


def restore_replicas(deployment):
    idled_at, replicas = deployment.metadata.annotations[IDLED_AT].split(',')
    log.debug(f'Restoring {replicas} replicas')
    deployment.spec.replicas = int(replicas)


def enable_ingress(ingress):
    log.debug('Enabling ingress')
    ingress.metadata.annotations[INGRESS_CLASS] = 'nginx'


def unmark_idled(deployment):
    log.debug('Removing idled annotation and label')

    deployment.metadata.labels = dict(
        filter(
            lambda label: label[0] != IDLED,
            deployment.metadata.labels.items()))

    deployment.metadata.annotations = dict(
        filter(
            lambda annotation: annotation[0] != IDLED_AT,
            deployment.metadata.annotations.items()))


@contextlib.contextmanager
def ingress(name, namespace, ingresses):
    ingress = ingresses[(name, namespace)]
    yield ingress
    write_ingress_changes(ingress)


def remove_host_rule(hostname, ingress):
    log.debug(
        f'Removing host rules for {hostname} '
        f'from ingress {ingress.metadata.name} '
        f'in namespace {ingress.metadata.namespace}')
    ingress.spec.rules = list(
        filter(
            lambda rule: rule.host != hostname,
            ingress.spec.rules))


if __name__ == '__main__':
    run(*sys.argv[1:])
