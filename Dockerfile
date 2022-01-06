ARG BUILD_FROM
FROM $BUILD_FROM

WORKDIR /app

# Install requirements for add-on
RUN \
  apk add --no-cache \
    python3 bluez py-pip

# py3-pip

# Copy data for add-on
COPY run.sh run.sh
COPY main.py main.py
COPY requirements.txt requirements.txt

RUN chmod a+x run.sh
RUN pip3 install -r requirements.txt

COPY . .

CMD [ "./run.sh" ]
