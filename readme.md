# TCP to S2-WL-ST to MQTT

Pull data from Solis S2-WL-ST datalogger using TCP without additional hardware. Send the data to a MQTT server with Home Assistant auto discovery.

> [!WARNING]
> * I don't know much about MODBUS
> * I don't know anything about inverters
> * I don't know much about dataloggers
> * I don't know much Python
>
> **You have been warned!**

## Background/motivation
This journey started before I got my Solar panels installed. I knew I would get a Solis S5-GR3P15K inverter and the S2-WT-ST data logger. I use Home Assistant and I try to have all my devices working locally without relying on cloud solutions. So I started researching existing Home assistant integrations and other solutions to be prepared. I found many alternatives but many of them required extra hardware. But it seemed that the S2-WT-ST is one of few (only?) Solis dataloggers that could be used to get data over TCP. Seeing that a few people had success after getting the firmware updated to 10010117 or higher made me confident that I would have this up and running quickly.

When the Solar panels, inverter and datalogger was installed I realized that it wasn't as easy as I had hoped. I tried the different integrations and solutions but nothing worked. I got the firmware updated to 10010125 and the support person informed me that what I was trying to do would probably not work. I tried again but still nothing worked. I started looking at simpler ways of trying to talk to the datalogger over TCP to be able to debug and run tests and found a few example script written in Python. I was able to connect to the stick but none the registers used in different integrations was working.

Then I found [solis2mqtt](https://github.com/incub77/solis2mqtt), it used a completely different set of registers and now I was able to make progress. I could match the data I got from the logger (still using my own test scripts) to what I could see in the Solis cloud app!

I started to set up solis2mqtt but quickly realized that it was not using TCP. Now you might ask, why not contribute to that project and add TCP support. The short answer is that the MODBUS library used does not support TCP so it would require a larger rewrite. I also see this project as a way of learning about both MODBUS and Python.

Finally I found a PDF that seems to have the correct registers listed. It's available in the `reference` folder. Using this PDF it will be possible to add even more sensors than those that I found referenced in the solis2mqtt project.

So many hours later, here we are. Debug code turned in to a hack turning into a hobby project.

So will this project work for you? I have no idea. I'm just hacking away on something to make it work for me and at the same time publish it here and by doing this I might be able to help someone else in the same situation.

## Credits
I've used the [solis2mqtt](https://github.com/incub77/solis2mqtt) repo as an inspiration and I've borrowed some code since that project does a lot of similar things to what I want to do.

## Requirements
* Only tested with a Solis S2-WL-ST datalogger with firmware 10010125 and a Solis S5-GR3P15K inverter with firmware 83003A
* A static IP on the data logger
* Docker
* MQTT server

## Questions & Answers
### Does Solis Cloud still work?

Most other integrations that I found all stated that Solis Cloud would not work when polling the datalogger over MODBUS. In my brief and short testing Solid Cloud still works, but not perfectly. I'm not sure if the newer firmware version makes a difference here or why its working. By "not perfectly" I mean that I can see that Solis Cloud is not always updated every 5 minutes. Sometimes it takes 15-20 minutes between updates but I've always seen that the datalogger sooner or later are able to send data to Solis Cloud. And if I stop the script/Docker container I always seen an update in Solid cloud within 10 minutes without having to restart the datalogger.

### Is it possible to control the inverter?

I have not focused on that since I don't have a need for it. If its possible to do so via MODBUS then it should be possible to add that functionality.

### Will this work with other dataloggers, inverters or firmware versions?

I have no idea. I will probably never change to another datalogger or upgrade the firmware unless I absolutely have to. If you are able to get this working on another datalogger, inverter or firmware version, please let me know and I will add the information to the repo!

### Will there be a Home Assistant (HACS) integration?

Maybe, I have no plans for it at the moment. My main goal is to get the data into Home Assistant and I decided to use MQTT to simlify the project a little bit.

## Getting started
**TODO**
