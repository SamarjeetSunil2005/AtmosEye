# AtmosEye: Smart ML-Powered Environmental Monitoring System

[![Python](https://img.shields.io/badge/Python-3.9%2B-blue.svg)](https://www.python.org/)
[![Flask](https://img.shields.io/badge/Flask-Backend-green.svg)](https://flask.palletsprojects.com/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Raspberry Pi](https://img.shields.io/badge/Hardware-Raspberry%20Pi%20Zero%202%20W-red.svg)](https://www.raspberrypi.com/)
[![Machine Learning](https://img.shields.io/badge/ML-Scikit--learn-orange.svg)](https://scikit-learn.org/)

---

## Table of Contents
- [Overview](#overview)
- [Features and Capabilities](#features-and-capabilities)
  - [Comprehensive Sensing and Compensation](#1-comprehensive-sensing-and-compensation)
  - [Machine Learning Intelligence](#2-machine-learning-intelligence)
  - [User Interfaces and Alerts](#3-user-interfaces-and-alerts)
  - [System Management](#4-system-management)
- [Hardware and Software Architecture](#hardware-and-software-architecture)
- [Software Stack](#software-stack)
- [Installation and Setup](#installation-and-setup)
  - [Install Dependencies](#install-dependencies)
  - [Hardware-Dependent Libraries](#hardware-dependent-libraries)
  - [Configuration](#configuration)
  - [Running the System](#running-the-system)
  - [Accessing the Dashboard](#accessing-the-dashboard)
- [Contribution and Future Development](#contribution-and-future-development)
- [License](#license)
- [Author](#author)

---

## Overview

**AtmosEye** is a cost-effective, portable IoT solution for comprehensive Indoor Air Quality (IAQ) and environmental monitoring.  
Built around the **Raspberry Pi Zero 2 W**, it leverages advanced sensors and Machine Learning (ML) to deliver real-time, actionable insights.

**Goal:** To bridge the gap between expensive professional systems and less accurate consumer-grade devices.

---

## Features and Capabilities

### 1. Comprehensive Sensing and Compensation

**Dual-Sensor Configuration:**
- **Bosch BME688:** Temperature, humidity, pressure, and gas measurements  
- **PMSA003-C:** Particulate matter detection (PM1.0, PM2.5, PM10)

**Environmental Metrics**
- Temperature  
- Humidity  
- Pressure  

**Air Quality Metrics**
- Indoor Air Quality (IAQ) Index  
- Volatile Organic Compound (VOC) Index  
- CO₂ Equivalent (ppm)  
- Particulate Matter (PM1.0, PM2.5, PM10 in µg/m³)

---

### 2. Machine Learning Intelligence

AtmosEye uses ML models developed with Scikit-learn to analyze and predict environmental patterns.

- **AtmosVision (Predictive Trend Analysis):**  
  AI-based forecasting of environmental trends.  
  Implemented in `prediction.py`.

- **AtmosInsights (Intelligent Summaries):**  
  Generates human-readable summaries of air quality trends.  
  Implemented in `atmosinsight.py`.

---

### 3. User Interfaces and Alerts

- **Progressive Web App (PWA):**  
  Offline-first dashboard (`dashboard.html`) served by Flask (`app.py`) for real-time visualization and device control.

- **Telegram Bot:**  
  (`telegram_bot.py`) Sends air quality alerts and allows live data requests.

- **Audible Alerts:**  
  Passive buzzer provides sound notifications for critical IAQ warnings.

---

### 4. System Management

- **Wi-Fi Management:**  
  `wifimanager.py` handles local network management directly from the device.

- **Data Logging and Maintenance:**  
  Data is stored in compressed CSV files organized by date.  
  Includes an automatic log maintenance system for long-term operation.

---

## Hardware and Software Architecture

The system follows a modular, microservice-like architecture, running entirely on the Raspberry Pi Zero 2 W.

### Hardware Requirements

| Component | Purpose |
|------------|----------|
| Raspberry Pi Zero 2 W | Main processing unit running backend and ML models |
| Bosch BME688 Sensor | Gas and environmental sensor for IAQ and VOC readings |
| PMSA003-C Sensor | Laser-based particulate matter sensor |
| Passive Buzzer | Audible alerts for smoke and critical air quality warnings |

---

## Software Stack

| Module | Technology | Function |
|--------|-------------|-----------|
| Data Acquisition | `iaqcalc.py` | Sensor interfacing, smoothing, and metric computation |
| Backend / API | Flask (`app.py`) | Serves dashboard and provides REST API endpoints |
| ML / Analysis | Scikit-learn, Pandas (`prediction.py`) | Predictive trend analysis using ML models |
| Frontend | Alpine.js, TailwindCSS (`dashboard.html`) | Responsive dashboard for data visualization |
| Remote Service | `telegram_bot.py` | Two-way Telegram communication for alerts and commands |

---

## Installation and Setup

### Install Dependencies

**Core Python Dependencies:**

```bash
pip install Flask pandas numpy scikit-learn psutil python-telegram-bot
```
### Hardware-Dependent Libraries

Install hardware-specific libraries using pip:

```bash
pip install adafruit-circuitpython-bme680 pms5003 RPi.GPIO
```
### Configuration
Clone the Repository
```bash
git clone https://github.com/SamarjeetSunil2005/AtmosEye.git
cd AtmosEye
```
Set Telegram Credentials
```bash
cp telegram_settings.example.py telegram_settings.py
nano telegram_settings.py
# Enter BOT_TOKEN and CHAT_ID
```
Set up HTTPS Certificates (required for PWA)
```bash
# Generate self-signed certificates
openssl req -x509 -newkey rsa:4096 -keyout key.pem -out cert.pem -days 365 -nodes
```
### Running the System

Start the Flask backend (sudo required for GPIO and Wi-Fi):
```bash
sudo python3 app.py
```
### Accessing the Dashboard

Once the server is running, open a browser on any device on the same network and navigate to:
```bash
https://[Raspberry_Pi_IP_Address]:5000
```
## Contribution and Future Development

Contributions, issues, and feature requests are welcome.

## Future Development Scope

- Cloud-based monitoring and multi-device data aggregation

- Native Android/iOS app integration for alerts and monitoring

- Additional gas sensors (O₃, NO₂, etc.)

- External weather data integration for advanced analysis

## License
This project is licensed under the MIT License.
See the LICENSE

## Author

**Developed by:**  
[![GitHub – Samarjeet Sunil](https://img.shields.io/badge/GitHub-Samarjeet%20Sunil-black?logo=github)](https://github.com/SamarjeetSunil2005)
