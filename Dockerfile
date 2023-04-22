ARG BUILD_FROM
FROM $BUILD_FROM

WORKDIR /app

# Install requirements for add-on
# (alpine image)
# RUN apk add --no-cache \
#    python3 bluez py-pip git

RUN apk add python3
RUN apk add bluez #< 5.66-r4"
    # https://pkgs.alpinelinux.org/packages?name=bluez&branch=v3.16&repo=&arch=aarch64&maintainer=
RUN apk add py-pip
RUN apk add git



# py3-pip

# Copy data for add-on
COPY run.sh run.sh
COPY main.py main.py
COPY requirements.txt requirements.txt
COPY . .

RUN pip3 install -r requirements.txt
RUN chmod a+x run.sh

CMD [ "./run.sh" ]
