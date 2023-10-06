# Building and running `batmon-ha` via Docker

## Step #1: Build the Docker image

If you are building for a regular x86 ('amd64') architecture run:

    git clone https://github.com/fl4p/batmon-ha.git
    cd batmon-ha
    docker buildx build --build-arg BUILD_FROM=homeassistant/amd64-base:latest -t batmon-ha "." 


If you're building `homeassistant/amd64-base:latest` with `homeassistant/aarch64-base:latest` if you're building for an Raspberry Pi.


## Step #2: Create the base configuration

Create a `options.json` configuration file, i.e. in the `batmon-ha` folder. 
Use the provided example `doc/options.json.template` and adjust as needed. 
Refer to the configuration section in `README.md` for more information.

    cp doc/options.json.template options.json
    nano options.json


### Step #3a: Run container manually

    docker run -d --privileged --restart unless-stopped --name batmon-ha -v /var/run/dbus/:/var/run/dbus/:z -v $PWD/options.json:/data/options.json:ro batmon-ha

### Step #3b: Run via docker-compose

You can also declare `batmon-ha` via an entry in your `docker-compose` file, i.e.:

        batmon-ha:
            container_name: batmon-ha
            image: batmon-ha
            restart: unless-stopped
            privileged: true
            volumes:
            - /path/tooptions.json:/app/options.json:ro
            - /var/run/dbus/:/var/run/dbus/:z

