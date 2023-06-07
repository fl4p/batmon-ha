FROM alpine

WORKDIR /app

RUN apk add --no-cache git py-pip python3 bluez #< 5.66-r4"
    # https://pkgs.alpinelinux.org/packages?name=bluez&branch=v3.16&repo=&arch=aarch64&maintainer=

# Copy data for add-on
COPY requirements.txt requirements.txt

RUN pip3 install -r requirements.txt

# copy app source as last step prevents rebuilding the whole image on code update.
COPY . .

CMD [ "sh", "run.sh" ]
