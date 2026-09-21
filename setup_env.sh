#!/usr/bin/env bash

# oci_config_setup.sh
# This script sets up the OCI configuration interactively and creates an oci.env file.

display_choices(){
cat <<EOF
Choose one of the two free shapes

1. VM.Standard.A1.Flex
2. VM.Standard.E2.1.Micro
EOF
}

read -p "Type name of the instance: " INSTANCE_NAME
clear

# Create the VM SSH key pair once on the machine running the hunter.
# The public key is reused for every launch attempt; the private key never needs
# to be sent to OCI or GitHub Actions.
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KEY_DIR="$PROJECT_DIR"
KEY_PRIVATE="$KEY_DIR/id_rsa"
KEY_PUBLIC="$KEY_DIR/id_rsa.pub"
mkdir -p "$KEY_DIR"
if [[ ! -f "$KEY_PUBLIC" || ! -f "$KEY_PRIVATE" ]]; then
    if ! command -v ssh-keygen >/dev/null 2>&1; then
        echo "ssh-keygen is required to create the VM SSH key pair." >&2
        exit 1
    fi
    echo "Creating persistent VM SSH key pair at $KEY_PRIVATE / $KEY_PUBLIC"
    ssh-keygen -t ed25519 -N "" -f "$KEY_PRIVATE" -C "oracle-freetier-instance" >/dev/null
    chmod 600 "$KEY_PRIVATE"
    chmod 644 "$KEY_PUBLIC"
else
    echo "Using existing VM SSH key pair: $KEY_PUBLIC"
fi

while true; do
    display_choices
    read -p "Enter your choice (1 or 2): " SHAPE
    case $SHAPE in
        1) SHAPE="VM.Standard.A1.Flex"; break ;;
        2) SHAPE="VM.Standard.E2.1.Micro"; break ;;
        *) clear; echo "Invalid choice. Please try again. (CTRL+C to quit)" ;;
    esac
done
clear

while true; do
    read -p "Use the script for your second free tier Micro Instance? (y/n): " BOOL_MICRO
    BOOL_MICRO=$(echo "$BOOL_MICRO" | tr '[:upper:]' '[:lower:]')
    case $BOOL_MICRO in
        y) BOOL_MICRO="True"; break ;;
        n) BOOL_MICRO="False"; break ;;
        *) clear; echo "Invalid choice. Please try again (CTRL+C to quit)" ;;
    esac
done
clear

read -p "Enter the Subnet OCID (or press Enter to skip): " SUBNET_ID
clear
read -p "Enter the Image OCID (or press Enter to skip): " IMAGE_ID
clear
if [[ "$SHAPE" == "VM.Standard.A1.Flex" ]]; then
    DEFAULT_IMAGE_FILTER="Minimal-aarch64"
else
    DEFAULT_IMAGE_FILTER="minimal"
fi
read -p "Image name filter [$DEFAULT_IMAGE_FILTER]: " IMAGE_NAME_FILTER
IMAGE_NAME_FILTER=${IMAGE_NAME_FILTER:-$DEFAULT_IMAGE_FILTER}
clear
read -p "Boot volume size in GB [134]: " BOOT_VOLUME_SIZE
BOOT_VOLUME_SIZE=${BOOT_VOLUME_SIZE:-134}
clear
read -p "Boot volume VPUs per GB [120]: " BOOT_VOLUME_VPUS_PER_GB
BOOT_VOLUME_VPUS_PER_GB=${BOOT_VOLUME_VPUS_PER_GB:-120}
clear

while true; do
    read -p "Enable Gmail notification? (y/n): " BOOL_MAIL
    BOOL_MAIL=$(echo "$BOOL_MAIL" | tr '[:upper:]' '[:lower:]')
    case $BOOL_MAIL in
        y) BOOL_MAIL="True"; break ;;
        n) BOOL_MAIL="False"; break ;;
        *) clear; echo "Invalid choice. Please try again (CTRL+C to quit)" ;;
    esac
done
clear

if [[ $BOOL_MAIL == "True" ]]; then
    read -p "Enter your email: " EMAIL
    clear
    read -p "Enter email app password (16 characters without spaces): " EMAIL_PASS
    clear
fi

read -p "Enter Discord webhook URL (or press Enter to skip): " DISCORD_WEBHOOK
clear
read -p "Enter Telegram bot token (or press Enter to skip): " TELEGRAM_TOKEN
clear
read -p "Enter Telegram user ID (or press Enter to skip): " TELEGRAM_USER_ID
clear

if [ -f oci.env ]; then
    mv oci.env oci.env.bak
    echo "Existing oci.env file backed up as oci.env.bak"
fi

cat <<EOF > oci.env
# OCI Configuration
OCI_CONFIG=$PROJECT_DIR/oci_config
OCT_FREE_AD=AD-1
DISPLAY_NAME="$INSTANCE_NAME"
# The other free shape is AMD: VM.Standard.E2.1.Micro
OCI_COMPUTE_SHAPE=$SHAPE
SECOND_MICRO_INSTANCE=$BOOL_MICRO
REQUEST_WAIT_TIME_SECS=60
SSH_AUTHORIZED_KEYS_FILE=$PROJECT_DIR/id_rsa.pub
SSH_AUTHORIZED_KEYS=
# SUBNET_ID to use ONLY in case running in local or a non E2.1.Micro instance
OCI_SUBNET_ID="$SUBNET_ID"
OCI_IMAGE_ID="$IMAGE_ID"
# OCI_IMAGE_ID takes precedence over this filter
IMAGE_NAME_FILTER="$IMAGE_NAME_FILTER"
OPERATING_SYSTEM="Canonical Ubuntu"
OS_VERSION=24.04
ASSIGN_PUBLIC_IP=false
BOOT_VOLUME_SIZE=$BOOT_VOLUME_SIZE
BOOT_VOLUME_VPUS_PER_GB=$BOOT_VOLUME_VPUS_PER_GB

# Gmail Notification
NOTIFY_EMAIL=$BOOL_MAIL
EMAIL="$EMAIL"
EMAIL_PASSWORD="$EMAIL_PASS"

# Discord Notification (optional)
DISCORD_WEBHOOK="$DISCORD_WEBHOOK"

# Telegram Notification (optional)
TELEGRAM_TOKEN="$TELEGRAM_TOKEN"
TELEGRAM_USER_ID="$TELEGRAM_USER_ID"
EOF

echo "OCI env configuration saved to oci.env"
