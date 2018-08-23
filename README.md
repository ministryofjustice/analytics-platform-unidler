# analytics-platform-unidler
The unidler is a web app which listens for HTTP requests meant for idled deployments. On receiving a request, it restores the deployment replicas to the number prior to idling, and redirects future requests to the deployment.

## How does it work?

1. Receive an HTTP request with an `X-Forwarded-Host` header value
2. If the deployment matching the hostname is currently unidling:
   1. If the deployment is now ready, redirect traffic to it
3. Otherwise, unidle the deployment by:
   1. Restoring the number of replicas to the number prior to idling
   2. Remove the "idled" label and annotation on the deployment
4. Finally, display a "please wait" message

## Testing

Build the docker image to run the tests:
```sh
docker build -t unidler .
```

## Deployment
Deploy to the kubernetes cluster using the [Helm chart](https://github.com/ministryofjustice/analytics-platform-helm-charts/tree/master/charts/unidler)
