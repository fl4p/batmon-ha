FROM alpine

WORKDIR /app

RUN apk add --no-cache py-pip python3 bluez #< 5.66-r4"
    # https://pkgs.alpinelinux.org/packages?name=bluez&branch=v3.16&repo=&arch=aarch64&maintainer=

# Copy data for add-on
COPY run.sh run.sh
COPY main.py main.py
COPY requirements.txt requirements.txt
COPY . .

RUN pip3 install -r requirements.txt
# RUN chmod a+x run.sh

CMD [ "sh", "run.sh" ]
