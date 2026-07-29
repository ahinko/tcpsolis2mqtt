# TCP to S2-WL-ST to MQTT

Pull data from Solis S2-WL-ST data logger using TCP without additional hardware. Send the data to a MQTT server with Home Assistant auto discovery.

> [!WARNING]
> * I don't know much about MODBUS
> * I don't know anything about inverters
> * I don't know much about data loggers
> * I don't know much Python
>
> **You have been warned!**

## Background/motivation
This journey started before I got my Solar panels installed. I knew I would get a Solis S5-GR3P15K inverter and the S2-WT-ST data logger. I use Home Assistant and I try to have all my devices working locally without relying on cloud solutions. So I started researching existing Home assistant integrations and other solutions to be prepared. I found many alternatives but many of them required extra hardware. But it seemed that the S2-WT-ST is one of few (only?) Solis data loggers that could be used to get data over TCP. Seeing that a few people had success after getting the firmware updated to 10010117 or higher made me confident that I would have this up and running quickly.

When the Solar panels, inverter and data logger was installed I realised that it wasn't as easy as I had hoped. I tried the different integrations and solutions but nothing worked. I got the firmware updated to 10010125 and the support person informed me that what I was trying to do would probably not work. I tried again but still nothing worked. I started looking at simpler ways of trying to talk to the data logger over TCP to be able to debug and run tests and found a few example script written in Python. I was able to connect to the stick but none the registers used in different integrations was working.

Then I found [solis2mqtt](https://github.com/incub77/solis2mqtt), it used a completely different set of registers and now I was able to make progress. I could match the data I got from the logger (still using my own test scripts) to what I could see in the Solis cloud app!

I started to set up solis2mqtt but quickly realised that it was not using TCP. Now you might ask, why not contribute to that project and add TCP support. The short answer is that the MODBUS library used does not support TCP so it would require a larger rewrite. I also see this project as a way of learning about both MODBUS and Python.

Finally I found a PDF that seems to have the correct registers listed. It's available in the `reference` folder. Using this PDF it will be possible to add even more sensors than those that I found referenced in the solis2mqtt project.

So many hours later, here we are. Debug code turned in to a hack turning into a hobby project.

So will this project work for you? I have no idea. I'm just hacking away on something to make it work for me and at the same time publish it here and by doing this I might be able to help someone else in the same situation.

## Credits
I've used the [solis2mqtt](https://github.com/incub77/solis2mqtt) repo as an inspiration and I've borrowed some code since that project does a lot of similar things to what I want to do.

## Requirements
* Only tested with a Solis S2-WL-ST data logger with firmware 10010125 and a Solis S5-GR3P15K inverter with firmware 83003A
* A static IP on the data logger
* Docker
* MQTT server

## Questions & Answers
### Why are there so few updates to the repo?

Because it works! If I for some reason would stop using this myself then I will archive this repo. So as long as the repo is in a "non archived" state then I'm using this myself and it works as expected even if there hasn't been any new versions in a while. I will keep dependencies up to date so a new minor/patch version will be available every now and then.

### Does Solis Cloud still work?

Most other integrations that I found all stated that Solis Cloud would not work when polling the data logger over MODBUS. In my brief and short testing Solid Cloud still works, but not perfectly. My guess is that most other integrations keeps the connection with the data logger alive while this project closes the connection when its done and then reconnects when it's time for the next update. By "not perfectly" I mean that I can see that Solis Cloud is not always updated every 5 minutes. Sometimes it takes 15-20 minutes between updates but I've always seen that the data logger sooner or later are able to send data to Solis Cloud. And if I stop the script/Docker container I always seen an update in Solid cloud within 10 minutes without having to restart the data logger.

### Is it possible to control the inverter?

I have not focused on that since I don't have a need for it. If its possible to do so via MODBUS then it should be possible to add that functionality.

### Will this work with other data loggers, inverters or firmware versions?

I have no idea. I will probably never change to another data logger or upgrade the firmware unless I absolutely have to. If you are able to get this working on another data logger, inverter or firmware version, please let me know and I will add the information to the repo!

### How do I update the firmware of the data logger and inverter?

I send an email to a support address for my country that I found here: https://www.solisinverters.com/se/contactus.html. I asked them to update my inverter and logger. I  included the models and serial number of the inverter and logger. It took less than 24 hours if I remember correctly.

### Will there be a Home Assistant (HACS) integration?

Maybe, I have no plans for it at the moment. My main goal is to get the data into Home Assistant and I decided to use MQTT to simplify the project a little bit.

## Getting started
Prepare a config file. Use `config.example.yaml` and modify it to your needs and save it as `config.yaml`. Most values should be self-explanatory. `register_chunks` is set to 20 by default but during my testing I've been able to query my datalogger for more than 80 registers at the same time.

### Upgrading to 3.0
**`inverter.max_power_kw` is now required and the container won't start without it.** Set it to the nameplate rating of your inverter, in kW:

```yaml
inverter:
  max_power_kw: 15
```

It used to default to 15, which is the rating of the inverter this project was written against. That default was a bad idea. Energy counter readings are rejected if they climb faster than this value allows, so on a 5 kW inverter the check was doing almost nothing, and the failure is silent — you'd only find out when a bogus reading reached Home Assistant and corrupted the statistics. Better to refuse to start than to guess.

It's a nameplate rating, not a tuning knob. If real readings are being rejected, the value isn't the problem.

### Environment variables
It's currently only possible to use environment variables to set MQTT user and password. Use `MQTT_USER` and `MQTT_PASSWORD`.

### MQTT topics
Every sensor is published to `<topic_prefix>/<sensor name>`, retained. On top of that there are three kinds of topic that aren't sensors:

| topic | what it is |
| --- | --- |
| `<topic_prefix>/availability` | `online` while the app is running. Set as the MQTT Last Will, so the broker sets it to `offline` if the container dies. Every sensor depends on it. |
| `<topic_prefix>/datalogger_availability` | `online` while the data logger answers. The voltage, frequency and temperature sensors depend on it, so they go unavailable at night instead of reporting a false 0. |
| `<topic_prefix>/_state/...` | Retained topics the app uses as its own storage, so a restart doesn't replay a counter reset. Not interesting to Home Assistant. |

When the data logger is unreachable, power and current are published as `0`, because that's true and because a Riemann sum helper integrating `active_power` needs the value to keep arriving. The energy counters are left showing their last value. Everything else goes unavailable.

### Why the energy readings are guarded
Energy registers aren't published as they arrive. The data logger intermittently serves a value belonging to a previous day, which Home Assistant records as real generation, so a morning could show over 100 kWh of production that never happened. What each register is checked against, and the measurements behind every decision, are in [docs/energy-guards.md](docs/energy-guards.md). Worth reading before changing anything under `app/` or the energy entries in `sensors.yaml`.

### Sensor configuration
`sensors.yaml` describes each register. Two fields drive more than they look like they do:

* `modbus.resets: daily | monthly | yearly` says when the inverter clears the register. Leaving it out means the register is a lifetime counter that may never decrease. It's how the app knows to publish a `0` at midnight rather than wait for an inverter that's asleep, and it requires `device_class: energy` with `state_class: total_increasing`.
* `homeassistant.state_class` on an energy sensor is either `total_increasing`, meaning a counter whose growth is checked against what the inverter could physically have generated, or empty, meaning a finished period's total that only moves at a rollover. Nothing else is accepted, and the app fails at startup if a sensor claims otherwise.

### Docker
`docker run -v "$(pwd)"/config.yaml:/usr/app/src/config.yaml:ro ghcr.io/ahinko/tcpsolis2mqtt:latest`

### Docker compose
```yaml
---
services:
  tcpsolis2mqtt:
    container_name: tcpsolis2mqtt
    image: ghcr.io/ahinko/tcpsolis2mqtt:latest
    restart: always
    volumes:
      - ./config.yaml:/usr/app/src/config.yaml:ro
```
