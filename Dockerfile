FROM python:3.11

# This is to normalize all day events, which don't have a timezone.
ENV TZ=America/Los_Angeles
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

ENV KUBECTL_VERSION v1.33.0
ADD https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl /usr/local/bin/kubectl
RUN chmod +x /usr/local/bin/kubectl

ENV PIP_NO_CACHE_DIR=1

COPY node-placeholder-scaler/requirements.txt /tmp/requirements.txt
RUN python3 -m pip install -r /tmp/requirements.txt
COPY node-placeholder-scaler/scaler /srv/scaler
WORKDIR /srv
ENTRYPOINT ["python3", "-m", "scaler"]
USER nobody
