ARG BUILD_FROM
FROM $BUILD_FROM

WORKDIR /app

# Install requirements for add-on
# (alpine image)
RUN \
  apk add --no-cache \
    python3 bluez py-pip git

# py3-pip

# Copy data for add-on
COPY run.sh run.sh
COPY main.py main.py
COPY requirements.txt requirements.txt

RUN chmod a+x run.sh
RUN pip3 install -r requirements.txt

COPY . .

CMD [ "./run.sh" ]
