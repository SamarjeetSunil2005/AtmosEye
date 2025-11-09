AtmosEye: Smart ML-Powered Environmental Monitoring System

AtmosEye is a cost-effective, portable Internet of Things (IoT) solution for comprehensive indoor air quality (IAQ) and environmental monitoring.
Built around the Raspberry Pi Zero 2 W, the system leverages advanced sensors and Machine Learning (ML) to provide real-time, actionable insights.

The primary goal of the project is to deliver an intelligent monitoring solution that bridges the gap between expensive professional systems and less accurate consumer devices.


---

Features and Capabilities

1. Comprehensive Sensing and Compensation

AtmosEye uses a dual-sensor configuration:

Bosch BME688 for temperature, humidity, pressure, and gas measurements.

PMSA003-C for particulate matter detection (PM1.0, PM2.5, PM10).


Environmental Metrics

Temperature

Humidity

Pressure


Air Quality Metrics

Indoor Air Quality (IAQ) Index

Volatile Organic Compound (VOC) Index

CO₂ Equivalent (ppm)

Particulate Matter (PM1.0, PM2.5, PM10 in µg/m³)



---

2. Machine Learning Intelligence

AtmosEye uses ML models developed with Scikit-learn to analyze and predict environmental patterns.

AtmosVision (Predictive Trend Analysis):
AI-based forecasting of environmental trends. Implemented in prediction.py.

AtmosInsights (Intelligent Summaries):
Generates human-readable summaries of air quality trends. Implemented in atmosinsight.py.



---

3. User Interfaces and Alerts

Designed for both local and remote access.

Progressive Web App (PWA):
A lightweight, offline-first dashboard (dashboard.html) served by a Flask backend (app.py) for real-time visualization and device control.

Two-Way Telegram Bot:
(telegram_bot.py) Sends air quality alerts and allows users to request live data.

Audible Alerts:
A passive buzzer provides audible notifications for critical IAQ warnings.



---

4. System Management

Wi-Fi Management:
wifimanager.py enables network management directly from the device.

Data Logging and Maintenance:
Sensor data is stored in compressed CSV files, organized by date.
Includes an automatic log maintenance system for long-term operation.



---

Hardware and Software Architecture

The system follows a modular, microservice-like architecture, running entirely on the Raspberry Pi Zero 2 W.

Hardware Requirements

Component	Purpose

Raspberry Pi Zero 2 W	Main processing unit running backend and ML models
Bosch BME688 Sensor	Gas and environmental sensor for IAQ and VOC readings
PMSA003-C Sensor	Laser-based particulate matter sensor
Passive Buzzer	Audible alerts for smoke and critical air quality warnings



---

Software Stack

Module	Technology	Function

Data Acquisition	iaqcalc.py (Python)	Sensor interfacing, smoothing, and metric computation
Backend / API	Flask (app.py)	Serves dashboard and provides REST API endpoints
ML / Analysis	Scikit-learn, Pandas (prediction.py)	Predictive trend analysis using ML models
Frontend	Alpine.js, TailwindCSS (dashboard.html)	Responsive web dashboard for data visualization
Remote Service	telegram_bot.py	Two-way communication via Telegram for alerts and commands



---

Installation and Setup

1. Prerequisites (on Raspberry Pi OS)

Enable required interfaces:

sudo raspi-config
# Enable I2C, Serial, and GPIO

Install dependencies:

# Core Python dependencies
pip install Flask pandas numpy scikit-learn psutil python-telegram-bot

# Hardware-dependent libraries
pip install adafruit-circuitpython-bme680 pms5003 RPi.GPIO


---

2. Configuration

Clone the repository:

git clone https://github.com/SamarjeetSunil2005/AtmosEye.git
cd AtmosEye

Set Telegram credentials:

cp telegram_settings.example.py telegram_settings.py
nano telegram_settings.py
# Enter BOT_TOKEN and CHAT_ID

Set up HTTPS certificates (required for PWA functionality):

# Generate self-signed certificates
openssl req -x509 -newkey rsa:4096 -keyout key.pem -out cert.pem -days 365 -nodes


---

3. Running the System

Start the Flask backend (sudo required for GPIO and Wi-Fi management):

sudo python3 app.py


---

4. Accessing the Dashboard

Once the server starts, open a browser on any device on the same network and navigate to:

https://[Raspberry_Pi_IP_Address]:5000


---

Contribution and Future Development

Contributions, issues, and feature requests are welcome.

Future Development Scope

Cloud Connectivity: Enable cloud-based monitoring and multi-device data aggregation.

Native App Integration: Develop Android/iOS apps for notifications and monitoring.

Specialized Sensors: Add support for additional gas sensors (O₃, NO₂, etc.).

Weather Correlation: Integrate external weather data for comprehensive analysis.



---

License

This project is licensed under the MIT License. See the LICENSE file for details.


---

Author

Developed by Samarjeet Sunil
GitHub: https://github.com/SamarjeetSunil2005
