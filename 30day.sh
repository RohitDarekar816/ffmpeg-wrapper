#!/bin/bash

# Directory containing audio files
AUDIO_DIR="/var/www/html/audio"

# Delete files older than 30 days
find "$AUDIO_DIR" -type f -mtime +30 -delete
