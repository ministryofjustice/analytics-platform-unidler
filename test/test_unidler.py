from io import BytesIO
from unittest.mock import MagicMock, call, patch

import pytest

import unidler
from unidler import (
    IDLED,
    IDLED_AT,
    INGRESS_CLASS,
    RequestHandler,
    UNIDLER,
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
def unidler_ingress():
    host_rule = MagicMock()
    host_rule.host = HOSTNAME
    ingress = MagicMock()
    ingress.spec.rules = [
        host_rule,
    ]
    ingress.metadata.name = UNIDLER
    ingress.metadata.namespace = 'default'
    return ingress


def test_remove_host_rule(unidler_ingress):
    assert any(rule.host == HOSTNAME for rule in unidler_ingress.spec.rules)

    unidler.remove_host_rule(HOSTNAME, unidler_ingress)

    assert all(rule.host != HOSTNAME for rule in unidler_ingress.spec.rules)


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

    with unidler.deployment_for_ingress(ingress):
        api.read_namespaced_deployment.assert_called_with(
            ingress.metadata.name,
            ingress.metadata.namespace)

    api.replace_namespaced_deployment.assert_called_with(
        deployment.metadata.name,
        deployment.metadata.namespace,
        deployment)


def test_write_ingress_changes(client, ingress):
    api = client.ExtensionsV1beta1Api.return_value

    unidler.write_ingress_changes(ingress)

    api.patch_namespaced_ingress.assert_called_with(
        ingress.metadata.name,
        ingress.metadata.namespace,
        ingress)


def test_ingress_for_host(client, ingress, unidler_ingress):
    api = client.ExtensionsV1beta1Api.return_value
    ingresses = {
        (ingress.metadata.name, ingress.metadata.namespace): ingress,
        (UNIDLER, 'default'): unidler_ingress,
    }
    with unidler.ingress_for_host(HOSTNAME, ingresses) as ing:
        assert ing.metadata.name == ingress.metadata.name
        assert ing.metadata.namespace == ingress.metadata.namespace

    api.patch_namespaced_ingress.assert_called_with(
        ingress.metadata.name,
        ingress.metadata.namespace,
        ingress)


def test_unidle_deployment(client, deployment, ingress, unidler_ingress):
    apps = client.AppsV1beta1Api.return_value
    apps.read_namespaced_deployment.return_value = deployment
    extensions = client.ExtensionsV1beta1Api.return_value

    with patch('unidler.build_ingress_lookup') as ingresses:
        ingresses.return_value = {
            (ingress.metadata.name, ingress.metadata.namespace): ingress,
            (UNIDLER, 'default'): unidler_ingress,
        }

        unidler.unidle_deployment(HOSTNAME)

        apps.replace_namespaced_deployment.assert_called_with(
            deployment.metadata.name,
            deployment.metadata.namespace,
            deployment)
        assert IDLED not in deployment.metadata.labels
        assert IDLED_AT not in deployment.metadata.annotations
        assert deployment.spec.replicas == 2

        assert extensions.patch_namespaced_ingress.mock_calls == [
            call(
                unidler_ingress.metadata.name,
                unidler_ingress.metadata.namespace,
                unidler_ingress),
            call(
                ingress.metadata.name,
                ingress.metadata.namespace,
                ingress),
        ]
        assert ingress.metadata.annotations[INGRESS_CLASS] == 'nginx'

        assert len(list(filter(
            lambda rule: rule.host == HOSTNAME,
            unidler_ingress.spec.rules))) == 0


class TestRequestHandler(object):

    def test_doGET(self, client, deployment, ingress, unidler_ingress):
        assert IDLED_AT in deployment.metadata.annotations

        apps = client.AppsV1beta1Api.return_value
        extensions = client.ExtensionsV1beta1Api.return_value

        ingresses = MagicMock()
        ingresses.items = [ingress, unidler_ingress]
        extensions.list_ingress_for_all_namespaces.return_value = ingresses

        apps.read_namespaced_deployment.return_value = deployment

        response = self.handle_request('GET', '/', {
            'X-Forwarded-Host': HOSTNAME,
        })
        assert response.status_code == 503
        assert IDLED not in deployment.metadata.labels
        assert IDLED_AT not in deployment.metadata.annotations
        assert deployment.spec.replicas == 2
        assert ingress.metadata.annotations[INGRESS_CLASS] == 'nginx'

        assert len(list(filter(
            lambda rule: rule.host == HOSTNAME,
            unidler_ingress.spec.rules))) == 0

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
