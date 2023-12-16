ARG BUILD_FROM
FROM $BUILD_FROM

WORKDIR /app

# Install requirements for add-on
# (alpine image)
# RUN apk add --no-cache python3 bluez py-pip git

RUN apk add python3
RUN apk add bluez #< 5.66-r4"
    # https://pkgs.alpinelinux.org/packages?name=bluez&branch=v3.16&repo=&arch=aarch64&maintainer=
RUN apk add py-pip
RUN apk add git
# py3-pip

# copy files
COPY . .

RUN python3 -m venv venv
RUN venv/bin/pip3 install -r requirements.txt
RUN . venv/bin/activate

RUN chmod a+x addon_main.sh

CMD ["./addon_main.sh" ]
