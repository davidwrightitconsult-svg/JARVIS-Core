#!/usr/bin/env bash
set -euo pipefail
sudo apt update
sudo apt upgrade -y
sudo apt install -y git python3 python3-venv python3-pip build-essential curl unzip htop ufw chrony
sudo systemctl enable --now chrony
sudo ufw allow OpenSSH
sudo ufw --force enable
mkdir -p "$HOME/friday"
echo 'Foundation packages installed. Repository cloning and application setup are performed after GitHub authentication.'
