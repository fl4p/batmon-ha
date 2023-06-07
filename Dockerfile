FROM alpine

WORKDIR /app

RUN apk add --no-cache py-pip python3 bluez #< 5.66-r4"
    # https://pkgs.alpinelinux.org/packages?name=bluez&branch=v3.16&repo=&arch=aarch64&maintainer=

# Copy data for add-on
COPY requirements.txt requirements.txt

RUN pip3 install -r requirements.txt

COPY . .

CMD [ "sh", "run.sh" ]
