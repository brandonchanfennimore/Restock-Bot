# Online Retail Stock Monitoring Bot

An automated monitoring system that tracks product availability across online retailers and delivers real-time notifications through Discord integration.

## Overview

This project was developed to automate stock monitoring for specific products across retail websites. The system periodically checks store pages, detects stock status changes, and posts alerts to a designated Discord channel.

## Features

- Automated product availability monitoring
- Conditional detection logic for stock status changes
- Real-time Discord notifications via bot integration
- Custom commands including:
  - `!help`
  - `!addwatch`
  - `!addstore`
- Configurable product tracking

## Architecture

The system follows a modular polling design:

- Monitoring module handles periodic page checks
- Detection logic evaluates stock state transitions
- Discord bot module manages command handling and message delivery

The polling system is capable of monitoring multiple retailers concurrently at fixed intervals.

## Requirements

- Python
- Discord API
- Persistent runtime environment (server or local machine)

## Future Improvements

- Deployment to cloud-based hosting
- Asynchronous request handling for improved efficiency
- Persistent database integration
- Enhanced rate-limiting and error handling
