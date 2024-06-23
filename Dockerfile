FROM node:20

WORKDIR /usr/src/app

COPY info.json .
COPY violentmonkey.env .

RUN apt-get update && \
  DEBIAN_FRONTEND=noninteractive apt-get install -y git curl jq && \
  VERSION="$(jq -r '.version' info.json)" && \
  LINK="$(jq -r '.link' info.json)" && \
  curl --output violentmonkey.xpi.zip -fsSL "$LINK" && \
  unzip -d violentmonkey-amo violentmonkey.xpi.zip && \
  rm -rf violentmonkey-amo/META-INF && \
  git clone --depth 1 -b "v$VERSION" https://github.com/violentmonkey/violentmonkey && \
  cd violentmonkey && \
  rm -rf .git && \
  cat ../violentmonkey.env | base64 -d > .env && \
  yarn && \
  yarn build && \
  cd .. && \
  diff -rq violentmonkey-amo violentmonkey/dist
