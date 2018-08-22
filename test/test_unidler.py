from io import BytesIO
from http import HTTPStatus
from unittest.mock import MagicMock, call, patch

import pytest

import unidler
from unidler import (
    IDLED,
    IDLED_AT,
    INGRESS_CLASS,
    RequestHandler,
    UNIDLER,
    UNIDLER_NAMESPACE,
    Unidling,
)


HOSTNAME = 'test.host.name'


@pytest.yield_fixture
def client():
    client = MagicMock()
    with patch('unidler.client', client):
        yield client


@pytest.fixture
def deployment():
    deployment = MagicMock()
    deployment.spec.replicas = 0
    deployment.metadata.labels = {
        IDLED: 'true',
    }
    deployment.metadata.annotations = {
        IDLED_AT: 'YYYY-mm-ddTHH:MM:SS+0000,2',
    }
    deployment.metadata.name = 'test-app'
    deployment.metadata.namespace = 'test-namespace'
    deployment.status.available_replicas = 0
    return deployment


@pytest.fixture
def ingress():
    host_rule = MagicMock()
    host_rule.host = HOSTNAME
    ingress = MagicMock()
    ingress.spec.rules = [
        host_rule,
    ]
    ingress.metadata.annotations = {
        INGRESS_CLASS: 'disabled',
    }
    ingress.metadata.name = 'test-app'
    ingress.metadata.namespace = 'test-namespace'
    return ingress


@pytest.fixture
def unidler_ingress(client):
    api = client.ExtensionsV1beta1Api.return_value
    ingress = api.read_namespaced_ingress.return_value
    host_rule = MagicMock()
    host_rule.host = HOSTNAME
    ingress.spec.rules = [
        host_rule,
    ]
    ingress.spec.tls = [MagicMock()]
    ingress.spec.tls[0].hosts = [
        HOSTNAME,
    ]
    ingress.metadata.name = UNIDLER
    ingress.metadata.namespace = UNIDLER_NAMESPACE
    return ingress


def test_remove_host_rule(unidler_ingress):
    assert any(rule.host == HOSTNAME for rule in unidler_ingress.spec.rules)
    assert any(host == HOSTNAME for host in unidler_ingress.spec.tls[0].hosts)

    unidler.remove_host_rule(HOSTNAME, unidler_ingress)

    assert all(rule.host != HOSTNAME for rule in unidler_ingress.spec.rules)
    assert all(host != HOSTNAME for host in unidler_ingress.spec.tls[0].hosts)


def test_unmark_idled(deployment):
    assert IDLED in deployment.metadata.labels
    assert IDLED_AT in deployment.metadata.annotations

    unidler.unmark_idled(deployment)

    assert IDLED not in deployment.metadata.labels
    assert IDLED_AT not in deployment.metadata.annotations


def test_enable_ingress(ingress):
    assert ingress.metadata.annotations[INGRESS_CLASS] == 'disabled'

    unidler.enable_ingress(ingress)

    assert ingress.metadata.annotations[INGRESS_CLASS] == 'nginx'


def test_restore_replicas(deployment):
    assert deployment.spec.replicas == 0
    assert deployment.metadata.annotations[IDLED_AT].split(',')[1] == '2'

    unidler.restore_replicas(deployment)

    assert deployment.spec.replicas == 2


def test_write_deployment_changes(client, deployment):
    unidler.write_deployment_changes(deployment)

    api = client.AppsV1beta1Api.return_value
    api.replace_namespaced_deployment.assert_called_with(
        deployment.metadata.name,
        deployment.metadata.namespace,
        deployment)


def test_deployment_for_ingress(client, deployment, ingress):
    api = client.AppsV1beta1Api.return_value
    api.read_namespaced_deployment.return_value = deployment

    deployment = unidler.deployment_for_ingress(ingress)
    api.read_namespaced_deployment.assert_called_with(
        ingress.metadata.name,
        ingress.metadata.namespace)


def test_write_ingress_changes(client, ingress):
    api = client.ExtensionsV1beta1Api.return_value

    unidler.write_ingress_changes(ingress)

    api.patch_namespaced_ingress.assert_called_with(
        ingress.metadata.name,
        ingress.metadata.namespace,
        ingress)


def test_ingress_for_host(client, ingress, unidler_ingress):
    api = client.ExtensionsV1beta1Api.return_value
    ingresses = api.list_ingress_for_all_namespaces.return_value
    ingresses.items = [
        ingress,
        unidler_ingress,
    ]
    ing = unidler.ingress_for_host(HOSTNAME)
    assert ing.metadata.name == ingress.metadata.name
    assert ing.metadata.namespace == ingress.metadata.namespace


def test_unidling_start(client, deployment, ingress):
    apps = client.AppsV1beta1Api.return_value
    apps.read_namespaced_deployment.return_value = deployment
    extensions = client.ExtensionsV1beta1Api.return_value
    extensions.list_ingress_for_all_namespaces.return_value.items = [
        ingress
    ]

    unidling = Unidling(HOSTNAME)
    unidling.start()

    apps.replace_namespaced_deployment.assert_called_with(
        deployment.metadata.name,
        deployment.metadata.namespace,
        deployment)
    assert IDLED not in deployment.metadata.labels
    assert IDLED_AT not in deployment.metadata.annotations
    assert deployment.spec.replicas == 2


class TestRequestHandler(object):

    def test_doGET(self, client, deployment, ingress):
        assert IDLED_AT in deployment.metadata.annotations

        apps = client.AppsV1beta1Api.return_value
        extensions = client.ExtensionsV1beta1Api.return_value

        extensions.list_ingress_for_all_namespaces.return_value.items = [
            ingress
        ]

        apps.read_namespaced_deployment.return_value = deployment

        response = self.handle_request('GET', '/', {
            'X-Forwarded-Host': HOSTNAME,
        })
        assert response.status_code == HTTPStatus.ACCEPTED
        assert IDLED not in deployment.metadata.labels
        assert IDLED_AT not in deployment.metadata.annotations
        assert deployment.spec.replicas == 2

    def test_doGET_already_started(self, client, deployment, ingress):

        # deployment already marked as unidle
        deployment.metadata.labels = {}
        deployment.metadata.annotations = {}
        deployment.spec.replicas = 2

        api = client.AppsV1beta1Api.return_value
        api.read_namespaced_deployment.return_value = deployment

        unidling = Unidling(HOSTNAME)
        unidling.started = True
        unidling.ingress = ingress
        RequestHandler.unidling[HOSTNAME] = unidling

        response = self.handle_request('GET', '/', {
            'X-Forwarded-Host': HOSTNAME,
        })

        api = client.AppsV1beta1Api.return_value
        api.replace_namespaced_deployment.assert_not_called()

    def test_doGET_unidling_is_done(
            self, client, deployment, ingress, unidler_ingress):

        # deployment already marked as unidle
        deployment.metadata.labels = {}
        deployment.metadata.annotations = {}
        deployment.spec.replicas = 2
        deployment.status.available_replicas = 1

        api = client.AppsV1beta1Api.return_value
        api.read_namespaced_deployment.return_value = deployment
        ext = client.ExtensionsV1beta1Api.return_value
        ext.list_ingress_for_all_namespaces.return_value.items = [
            ingress,
        ]

        unidling = Unidling(HOSTNAME)
        unidling.started = True
        unidling.ingress = ingress
        RequestHandler.unidling[HOSTNAME] = unidling

        response = self.handle_request('GET', '/', {
            'X-Forwarded-Host': HOSTNAME,
        })

        assert len(list(filter(
            lambda rule: rule.host == HOSTNAME,
            unidler_ingress.spec.rules))) == 0

        assert ingress.metadata.annotations[INGRESS_CLASS] == 'nginx'

    def handle_request(self, method, path, headers={}):
        request = f'{method} {path} HTTP/1.0\n'
        request += '\n'.join(
            f'{header}: {value}' for header, value in headers.items())
        mock = MagicMock()
        mock.makefile.return_value = BytesIO(request.encode('iso-8859-1'))
        self.server = MagicMock()
        with patch('socketserver._SocketWriter') as SocketWriter:
            writer = SocketWriter.return_value
            self.handler = RequestHandler(mock, ('0.0.0.0', 8888), self.server)
            return self.parse_response(writer.write.mock_calls)

    def parse_response(self, calls):
        res = MagicMock()
        response = ''.join(
            call[1][0].decode('utf-8')
            for call in calls)
        response = response.split('\r\n')
        status, response = response[:1][0], response[1:]
        res.status = status = status.split(' ', 1)[1]
        res.status_code = int(status.split(' ')[0])
        res.headers = {}
        line, response = response[:1][0], response[1:]
        while line:
            res.headers.update((line.split(': ', 1),))
            line, response = response[:1][0], response[1:]
        res.body = ''.join(response)
        return res
