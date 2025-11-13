ARG BUILD_FROM
FROM $BUILD_FROM

WORKDIR /app

# Install requirements for add-on
# (alpine image)
# RUN apk add --no-cache python3 bluez py-pip git

RUN apk add python3~3.13 || apk add python3~3.12 || apk add python3
RUN apk add bluez
#RUN apk add bluez < 5.66-r4"
# https://pkgs.alpinelinux.org/packages?name=bluez&branch=v3.16&repo=&arch=aarch64&maintainer=
RUN apk add py-pip
RUN apk add git
# py3-pip

# copy files
COPY . .

# create a separate venv for a specific bleak version that has a pairing agent that can pair devices with a PSK
RUN python3 -m venv venv_bleak_pairing
RUN venv_bleak_pairing/bin/pip3 install -r requirements.txt
RUN venv_bleak_pairing/bin/pip3 install 'git+https://github.com/jpeters-ml/bleak@feature/windowsPairing' || true


RUN python3 -m venv venv
RUN venv/bin/pip3 install -r requirements.txt
RUN venv/bin/pip3 install influxdb || true
RUN venv/bin/pip3 install aiobmsble || true
RUN . venv/bin/activate

RUN chmod a+x addon_main.sh

CMD ["./addon_main.sh" ]
