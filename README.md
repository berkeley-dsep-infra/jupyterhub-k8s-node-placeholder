# Jupyterhub K8s Calendar Node Placeholder

This repository contains a Helm chart for deploying a JupyterHub Node Placeholder service on a Kubernetes cluster. The Node Placeholder service is designed to manage and allocate placeholder nodes for JupyterHub users, ensuring efficient resource utilization and improved user experience during surge login events.

The Node Placeholder service works by monitoring the academic calendar events from a provided iCal URL. Based on the events, it dynamically adjusts the number of placeholder nodes available in the Kubernetes cluster, allowing JupyterHub to pre-allocate resources for anticipated user logins. If there are existing user nodes deployed, and these have the capacity to handle additional users, the Node Placeholder service will scale down the number of placeholder nodes accordingly.

> [!NOTE]
> Be sure that the time zone of your public calendar matches the time zone set in the Helm chart values (`calendarTimezone`)!  Kubernetes itself runs by default in UTC time, but the Node Placeholder service needs to interpret the calendar events in the correct time zone to function properly.

If any existing nodes are underutilized, the Node Placeholder service will also scale down the number of placeholder nodes to free up resources. By default, it will not scale up a new placeholder node if any existing user nodes have > 20% of both CPU and RAM available. This is customizable in the the helm chart values (`values.yaml`).

The events are fetched from the iCal URL and parsed to determine peak usage times, such as the start of exam periods or other significant academic events where you expect a large number of users to log in. The service then scales the number of placeholder nodes accordingly, ensuring that users have a seamless experience when logging into JupyterHub during these high-demand periods.

When creating these events, they only need to be scheduled for the exact time periods where you expect a surge in logins. For instance, if you expect a surge of logins before a 9am exam, the event should be scheduled to start at 8:50am and end at 9:10am. This allows the Node Placeholder service to allocate resources just in time for the expected demand.

An example calendar entry for an event might look like this:

``` yaml
pool1: 2
pool2: 1
```

This indicates that during the event, 2 placeholder nodes should be allocated from `pool1` and 1 placeholder node from `pool2`.

## Installation

JupyterHub K8S Node Placeholder is installed as a Helm chart.

### Requirements

- Jupyterhub running on Kubernetes
- A publicly accessible iCal URL containing the academic calendar events (eg: Google Calendar, Outlook Calendar)
- Helm 3.x installed on your local machine or CI/CD pipeline

### Example Helm configuration

An example configuration in `values.yaml` might look like this:

``` yaml
# The URL of the public calendar to use for the node placeholder
calendarUrl: https://url.to/your/public/calendar.ics

calendarTimezone: "America/Los_Angeles"

scalingStrategy: mem  # Options: cpu, mem, balanced (default)
memoryThreshold: 35  # Spin up a new node quickly as all users spawn in to a single node pool

nodePools:
  # The short name of the node pool, used in the calendar event description.
  # In this example, we have a node pool named "user-pool" that contains our
  # Jupyterhub singleuser nodes:
  user:
    nodeSelector:
    hub.jupyter.org/pool-name: user-pool
    resources:
    requests:
        # Some value slightly lower than allocatable RAM on the nodepool in bytes
        # This is an example using a GCP n2-highmem-8 node with 64G of RAM allocatable
        memory: 60929654784
    replicas: 1
```

### Calculating the amount of RAM for each placeholder node

The memory request for each placeholder node should be set to a value slightly lower
than the allocatable RAM on the node pool. This ensures that the placeholder pods
can be scheduled on the nodes in the pool without exceeding their capacity, and
thereby triggering another node to be created.

The node placeholder pod should be 'big' enough that it needs to be kicked out to get even a single
user pod on the node - but not so big that it can't run on a node where other system pods are running!

1. Select a node in the node pool you want to deploy the placeholder to: `export NODE=<node-name>`
2. Get total amount of memory allocatable to pods on this node: `kubectl get node $NODE -o jsonpath='{.status.allocatable.memory}'`
3. Get total amount of memory used by non-user pods on the node:

``` bash
   kubectl get -A pod -l 'component!=user-placeholder' --field-selector spec.nodeName=$NODE -o jsonpath='{range .items[*].spec.containers[*]}{.name}{"\t"}{.resources.requests.memory}{"\n"}{end}' | egrep -v 'pause|notebook' | sort
```

4. This will return memory units you'll need to sum yourself. It's generally easiest to convert all values to bytes.
5. Subtract the output of (3) from the output of (2), and then subtract an additional 256Mi (or 277872640 bytes) for wiggle room.

This is an example using a GCP `n2-highmem-8` node with 64G of RAM allocatable:

``` bash
$ NODE=<node name>
$ kubectl get node ${NODE} -o jsonpath='{.status.allocatable.memory}' # convert to bytes
60055600Ki%
$ kubectl get -A pod -l 'component!=user-placeholder' \
  --field-selector spec.nodeName=${NODE} \
  -o jsonpath='{range .items[*].spec.containers[*]}{.name}{"\t"}{.resources.requests.memory}{"\n"}{end}' | \
  egrep -v 'pause|notebook' # convert all values to bytes for easier matching
calico-node
fluentbit       100Mi
fluentbit-gke   100Mi
gke-metrics-agent       60Mi
ip-masq-agent   16Mi
kube-proxy
prometheus-node-exporter
```

Subtract the sum of the second command's values from the first value, then subtract another 277872640 bytes for wiggle room:

In this case:  `(60055600Ki - (100Mi + 100Mi + 60Mi + 16Mi)) - 256Mi`

And in Bytes:  `(61496934400 - (104857600 + 104857600 + 16777216 + 62914560)) - 277872640 == 60929654784`

### Installation with Helm

After you've calculated the appropriate memory request for each placeholder node, and edited your `values.yaml` file, you can install the Helm chart using the following command:

``` bash
helm upgrade --install --namespace node-placeholder-scaler --create-namespace node-placeholder-scaler oci://>TBD>/node-placeholder-scaler --values values.yaml
```

## Determining CPU and RAM scaling thresholds

When running, no new placeholder nodes will be deployed if existing nodes have more than the default or user-defined threshold of CPU or RAM.

There are several factors in play for how you determine your scaling thresholds, consisting of your Jupyterhub deployment architecture, user resource usage (lots of RAM and a small amount of CPU), number of users per node, and potential user login "surges" (aka tens or hundreds of users logging in simultaneously, eg: for a class or seminar).

One should also have an idea of how long it takes for a new node to spin up.  This not only include the launching of the k8s system pods, but image pulling as well.  This can take about ~10 minutes for a GKE node and reasonably sized (~3G compressed) image.

the ultimate goal is to find a threshold that hits the sweet spot between minimizing extra placeholder nodes (and thereby keep additional node costs to a minimum), and spinning up a node in enough time such that user logins don't take more than a couple of minutes at most.

### Tens to hundreds of users with one or more hubs, single node pool

This will probably be the most common use case.

For a k8s architecture that has all user pods constrained to a single node pool, particularly with multiple Jupyterhubs deployed, and with a limit of roughly 70-80 users before a new node is spun up, it's probably best to start with the default memory or cpu threshold of 20%, and watch the placeholder-scaler pod logs during scaling events to see if you're starting placeholder nodes up early enough to keep login times down.

For example, the [Cal-ICOR](https://github.com/cal-icor/cal-icor-hubs) deployment has roughly 300-400 unique users per day and uses a `mem` scaling strategy set to spin up a placeholder node at 40% memory commitment.  This allows us to spin a node up early to catch burst login events, but will also tear it down quickly if the login pressure eases.

### Hundreds to thousands of users with more than one hub, multiple node pools

For larger deployments like [UC Berkeley's Datahub](https://github.com/dsep-infra/datahub) that support thousands of users per day (~15,000 per semester), most hubs have their own dedicated node pools.  This deployment typically has many users logged in around the clock, with more than one node per pool already serving users and providing extra headroom for surge logins.  They run the default `balanced` strategy and are happy with a 20% threshold.

Ultimately you will need to check the logs, and cross-reference when new nodes are spun up against the time it takes for a user to log in successfully.

## Working with this repository

You will need Python 3.11+ and `pip` installed on your system. You can manage Python
environments using tools like `venv` or `conda`.

For basic development, you will need to install the dependencies listed in
`dev-requirements.txt`. You can do this by running:

``` bash
pip install -r dev-requirements.txt
```

For more advanced development, including testing any changes you make, you will
need to install the dependencies listed in
[`requirements.txt`](node-placeholder-scaler/requirements.txt).

``` bash
pip install -r node-placeholder-scaler/requirements.txt
```

### Pre-Commit hooks: Installing

The previous step, `pip install -r dev-requirements.txt`, installs the package
[`pre-commit`](https://pre-commit.com/). This is used to run a series of
commands defined in the file [`.pre-commit-config.yaml`](.pre-commit-config.yaml)
to help ensure no mistakes are committed to the repo.

After you've installed `dev-requirements.txt`, execute the following two
commands:

``` bash
pre-commit install
pre-commit run --all-files
```

### Development

When working with this repo, you should create a fork and work on a feature
branch. When you're ready to submit your changes, create a pull request against
your fork, and open a PR against the `main` branch of this repo.

#### Changing the Python imports

If you need to change or update the scaler's Python imports, you will need to
recompile the scaler's `requirements.txt` file. You can do this by running:

``` bash
cd node-placeholder-scaler
pip-compile --output-file=requirements.txt requirements.in
```

Create a PR with your changes, and these will be added to the latest image created.

#### Testing your changes

The unit tests are located in `node-placeholder-scaler/tests`, and contain a
comprehensive suite of checks against both `scaler.py` and
`calendar_parser.py`.

These are run automatically [via this workflow](.github/workflows/test-scaler.yaml)
when a pull request is opened.

To manually run the Python unit tests, you need to install both `dev-requirements.txt` and
`node-placeholder-scaler/requirements.txt`.  After this, you can run:

``` bash
python node-placeholder-scaler/tests/run_tests.py
```
