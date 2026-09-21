import configparser
import itertools
import json
import logging
import os
import smtplib
import sys
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Union

import oci
import paramiko
from dotenv import load_dotenv
import requests

# Load environment variables from .env file
load_dotenv('oci.env')

ARM_SHAPE = "VM.Standard.A1.Flex"
E2_MICRO_SHAPE = "VM.Standard.E2.1.Micro"

# Access loaded environment variables and strip white spaces
OCI_CONFIG = os.getenv("OCI_CONFIG", "").strip()
OCT_FREE_AD = os.getenv("OCT_FREE_AD", "").strip()
DISPLAY_NAME = os.getenv("DISPLAY_NAME", "").strip()
WAIT_TIME = int(os.getenv("REQUEST_WAIT_TIME_SECS", "0").strip())
SSH_AUTHORIZED_KEYS_FILE = os.getenv("SSH_AUTHORIZED_KEYS_FILE", "").strip()
SSH_AUTHORIZED_KEYS = os.getenv("SSH_AUTHORIZED_KEYS", "").strip()
OCI_IMAGE_ID = os.getenv("OCI_IMAGE_ID", None).strip() if os.getenv("OCI_IMAGE_ID") else None
OCI_COMPUTE_SHAPE = os.getenv("OCI_COMPUTE_SHAPE", ARM_SHAPE).strip()
SECOND_MICRO_INSTANCE = os.getenv("SECOND_MICRO_INSTANCE", 'False').strip().lower() == 'true'
OCI_SUBNET_ID = os.getenv("OCI_SUBNET_ID", None).strip() if os.getenv("OCI_SUBNET_ID") else None
OPERATING_SYSTEM = os.getenv("OPERATING_SYSTEM", "").strip()
OS_VERSION = os.getenv("OS_VERSION", "").strip()
IMAGE_NAME_FILTER = os.getenv("IMAGE_NAME_FILTER", "Minimal-aarch64").strip()
ASSIGN_PUBLIC_IP = os.getenv("ASSIGN_PUBLIC_IP", "false").strip()
BOOT_VOLUME_SIZE = os.getenv("BOOT_VOLUME_SIZE", "50").strip()
BOOT_VOLUME_VPUS_PER_GB = os.getenv("BOOT_VOLUME_VPUS_PER_GB", "120").strip()
NOTIFY_EMAIL = os.getenv("NOTIFY_EMAIL", 'False').strip().lower() == 'true'
EMAIL = os.getenv("EMAIL", "").strip()
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", "").strip()
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "").strip()

try:
    boot_volume_vpus_per_gb = int(BOOT_VOLUME_VPUS_PER_GB)
except ValueError as exc:
    raise ValueError("BOOT_VOLUME_VPUS_PER_GB must be a positive integer") from exc
if boot_volume_vpus_per_gb <= 0:
    raise ValueError("BOOT_VOLUME_VPUS_PER_GB must be greater than zero")

# Read the configuration from oci_config file
config = configparser.ConfigParser()
try:
    config.read(OCI_CONFIG)
    OCI_USER_ID = config.get('DEFAULT', 'user')
    if OCI_COMPUTE_SHAPE not in (ARM_SHAPE, E2_MICRO_SHAPE):
        raise ValueError(f"{OCI_COMPUTE_SHAPE} is not an acceptable shape")
    env_has_spaces = any(isinstance(confg_var, str) and " " in confg_var
                        for confg_var in [OCI_CONFIG, OCT_FREE_AD, WAIT_TIME,
                                SSH_AUTHORIZED_KEYS_FILE, OCI_IMAGE_ID,
                                OCI_COMPUTE_SHAPE, SECOND_MICRO_INSTANCE,
                                OCI_SUBNET_ID, OS_VERSION, NOTIFY_EMAIL, EMAIL,
                                EMAIL_PASSWORD, DISCORD_WEBHOOK])
    config_has_spaces = any(' ' in value for section in config.sections()
                            for _, value in config.items(section))
    if env_has_spaces:
        raise ValueError("oci.env has spaces in values which is not acceptable")
    if config_has_spaces:
        raise ValueError("oci_config has spaces in values which is not acceptable")

except configparser.NoSectionError:
    msg = (
        "oci_config is missing the [DEFAULT] section. "
        "Ensure your config file matches the format shown in sample_oci_config."
    )
    with open("ERROR_IN_CONFIG.log", "w", encoding='utf-8') as file:
        file.write(msg)
    print(msg)

except configparser.NoOptionError:
    msg = (
        "oci_config is missing the 'user' key under [DEFAULT]. "
        "Copy the OCID from your OCI profile and add: user=ocid1.user.oc1..<your-ocid>. "
        "Refer to sample_oci_config for the expected format."
    )
    with open("ERROR_IN_CONFIG.log", "w", encoding='utf-8') as file:
        file.write(msg)
    print(msg)

except configparser.Error as e:
    with open("ERROR_IN_CONFIG.log", "w", encoding='utf-8') as file:
        file.write(str(e))

    print(f"Error reading the configuration file: {e}")

if not OCI_USER_ID:
    raise SystemExit("OCI configuration could not be loaded. See ERROR_IN_CONFIG.log for details.")

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("setup_and_info.log"),
        logging.StreamHandler(sys.stdout)
    ]
)
logging_step5 = logging.getLogger("launch_instance")
logging_step5.setLevel(logging.INFO)
fh = logging.FileHandler("launch_instance.log")
fh.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
logging_step5.addHandler(fh)

# Set up OCI Config and Clients
oci_config_path = OCI_CONFIG if OCI_CONFIG else "~/.oci/config"
config = oci.config.from_file(oci_config_path)
iam_client = oci.identity.IdentityClient(config)
network_client = oci.core.VirtualNetworkClient(config)
compute_client = oci.core.ComputeClient(config)

IMAGE_LIST_KEYS = [
    "lifecycle_state",
    "display_name",
    "id",
    "operating_system",
    "operating_system_version",
    "size_in_mbs",
    "time_created",
]


def write_into_file(file_path, data):
    """Write data into a file."""
    with open(file_path, mode="a", encoding="utf-8") as file_writer:
        file_writer.write(data)


def send_email(subject, body, email, password):
    """Send an HTML email using the SMTP protocol."""
    message = MIMEMultipart()
    message["Subject"] = subject
    message["From"] = email
    message["To"] = email
    html_body = MIMEText(body, "html")
    message.attach(html_body)

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        try:
            server.starttls()
            server.login(email, password)
            server.sendmail(email, email, message.as_string())
        except smtplib.SMTPException as mail_err:
            logging.error("Error while sending email: %s", mail_err)
            raise


def list_all_instances(compartment_id):
    """Retrieve all instances in the specified compartment."""
    return execute_oci_command(
        compute_client,
        "list_instances",
        compartment_id=compartment_id,
    )


def generate_html_body(instance):
    """Generate HTML body for the email with instance details."""
    with open('email_content.html', 'r', encoding='utf-8') as email_temp:
        html_template = email_temp.read()
    html_body = html_template.replace('&lt;INSTANCE_ID&gt;', instance.id)
    html_body = html_body.replace('&lt;DISPLAY_NAME&gt;', instance.display_name)
    html_body = html_body.replace('&lt;AD&gt;', instance.availability_domain)
    html_body = html_body.replace('&lt;SHAPE&gt;', instance.shape)
    html_body = html_body.replace('&lt;STATE&gt;', instance.lifecycle_state)
    return html_body


def create_instance_details_file_and_notify(instance, shape=ARM_SHAPE):
    """Create a file with instance details and notify the user."""
    details = [f"Instance ID: {instance.id}",
               f"Display Name: {instance.display_name}",
               f"Availability Domain: {instance.availability_domain}",
               f"Shape: {instance.shape}",
               f"State: {instance.lifecycle_state}",
               "\n"]
    micro_body = 'TWo Micro Instances are already existing and running'
    arm_body = '\n'.join(details)
    body = arm_body if shape == ARM_SHAPE else micro_body
    write_into_file('INSTANCE_CREATED', body)

    html_body = generate_html_body(instance)
    if NOTIFY_EMAIL:
        send_email('OCI INSTANCE CREATED', html_body, EMAIL, EMAIL_PASSWORD)


def notify_on_failure(failure_msg):
    """Notify users when instance creation fails due to an unhandled error."""
    mail_body = (
        "The script encountered an unhandled error and exited unexpectedly.\n\n"
        "Please re-run the script by executing './setup_init.sh rerun'.\n\n"
        "And raise a issue on GitHub if its not already existing:\n"
        "https://github.com/mohankumarpaluru/oracle-freetier-instance-creation/issues\n\n"
        " And include the following error message to help us investigate and resolve the problem:\n\n"
        f"{failure_msg}"
    )
    write_into_file('UNHANDLED_ERROR.log', mail_body)
    if NOTIFY_EMAIL:
        send_email('OCI INSTANCE CREATION SCRIPT: FAILED DUE TO AN ERROR', mail_body, EMAIL, EMAIL_PASSWORD)


def check_instance_state_and_write(compartment_id, shape, states=('RUNNING', 'PROVISIONING'),
                                   tries=3):
    """Check for a matching instance and notify when one is found."""
    for _ in range(tries):
        instance_list = list_all_instances(compartment_id=compartment_id)
        if shape == ARM_SHAPE:
            running_arm_instance = next((instance for instance in instance_list if
                                         instance.shape == shape and instance.lifecycle_state in states), None)
            if running_arm_instance:
                create_instance_details_file_and_notify(running_arm_instance, shape)
                return True
        else:
            micro_instance_list = [instance for instance in instance_list if
                                   instance.shape == shape and instance.lifecycle_state in states]
            if len(micro_instance_list) > 1 and SECOND_MICRO_INSTANCE:
                create_instance_details_file_and_notify(micro_instance_list[-1], shape)
                return True
            if len(micro_instance_list) == 1 and not SECOND_MICRO_INSTANCE:
                create_instance_details_file_and_notify(micro_instance_list[-1], shape)
                return True
        if tries - 1 > 0:
            time.sleep(60)
    return False


def handle_errors(command, data, log):
    """Handle errors and return after delaying retryable operations."""
    retryable_codes = {
        "TooManyRequests",
        "Out of host capacity.",
        "InternalError",
        "RequestException",
    }
    retryable_statuses = {502, 503, 504}
    retryable_messages = (
        "Out of host capacity.",
        "Bad Gateway",
        "Max retries exceeded",
        "ProxyError",
        "Tunnel connection failed",
        "Connection aborted",
        "ConnectTimeout",
        "Read timed out",
    )
    message = data.get("message", "")

    if (
        data.get("code") in retryable_codes
        or data.get("status") in retryable_statuses
        or any(retry_msg in message for retry_msg in retryable_messages)
    ):
        log.info("Command: %s--\nOutput: %s", command, data)
        time.sleep(max(10, WAIT_TIME))
        return True

    failure_msg = '\n'.join([f'{key}: {value}' for key, value in data.items()])
    notify_on_failure(failure_msg)
    raise Exception("Error: %s" % data)


def execute_oci_command(client, method, *args, **kwargs):
    """Execute an OCI command, preserving the existing retry behavior."""
    while True:
        try:
            response = getattr(client, method)(*args, **kwargs)
            data = response.data if hasattr(response, "data") else response
            return data
        except oci.exceptions.ServiceError as srv_err:
            data = {"status": srv_err.status,
                    "code": srv_err.code,
                    "message": srv_err.message}
            handle_errors(args, data, logging_step5)
        except oci.exceptions.RequestException as req_err:
            data = {
                "status": None,
                "code": "RequestException",
                "message": str(req_err),
            }
            handle_errors(args, data, logging_step5)


def generate_ssh_key_pair(public_key_file: Union[str, Path], private_key_file: Union[str, Path]):
    """Generate an SSH key pair and save it to the specified files."""
    key = paramiko.RSAKey.generate(2048)
    key.write_private_key_file(private_key_file)
    write_into_file(public_key_file, (f"ssh-rsa {key.get_base64()} "
                                      f"{Path(public_key_file).stem}_auto_generated"))


def read_or_generate_ssh_public_key(public_key_setting: str):
    """Return a raw SSH public key, or read/generate one from a file path."""
    if public_key_setting.startswith(("ssh-rsa ", "ssh-ed25519 ", "ecdsa-sha2-")):
        return public_key_setting

    if SSH_AUTHORIZED_KEYS.startswith(("ssh-rsa ", "ssh-ed25519 ", "ecdsa-sha2-")):
        return SSH_AUTHORIZED_KEYS

    if not public_key_setting:
        raise ValueError(
            "SSH_AUTHORIZED_KEYS_FILE is empty and SSH_AUTHORIZED_KEYS does not contain a raw SSH public key."
        )

    public_key_path = Path(public_key_setting)
    if not public_key_path.is_file():
        logging.info("SSH key doesn't exist... Generating SSH Key Pair")
        public_key_path.parent.mkdir(parents=True, exist_ok=True)
        private_key_path = public_key_path.with_name(f"{public_key_path.stem}_private")
        generate_ssh_key_pair(public_key_path, private_key_path)

    with open(public_key_path, "r", encoding="utf-8") as pub_key_file:
        return pub_key_file.read().strip()


def send_discord_message(message):
    """Send a message to Discord using the webhook URL if available."""
    if DISCORD_WEBHOOK:
        payload = {"content": message}
        try:
            response = requests.post(DISCORD_WEBHOOK, json=payload)
            response.raise_for_status()
        except requests.RequestException as e:
            logging.error("Failed to send Discord message: %s", e)


def launch_instance():
    """Launch an OCI Compute instance using the configured parameters."""
    user_info = execute_oci_command(iam_client, "get_user", OCI_USER_ID)
    oci_tenancy = user_info.compartment_id
    logging.info("OCI_TENANCY: %s", oci_tenancy)

    availability_domains = execute_oci_command(
        iam_client, "list_availability_domains", compartment_id=oci_tenancy)
    oci_ad_name = [item.name for item in availability_domains if
                   any(item.name.endswith(oct_ad) for oct_ad in OCT_FREE_AD.split(","))]
    oci_ad_names = itertools.cycle(oci_ad_name)
    logging.info("OCI_AD_NAME: %s", oci_ad_name)

    oci_subnet_id = OCI_SUBNET_ID
    if not oci_subnet_id:
        subnets = execute_oci_command(network_client, "list_subnets", compartment_id=oci_tenancy)
        oci_subnet_id = subnets[0].id
    logging.info("OCI_SUBNET_ID: %s", oci_subnet_id)

    image_display_name = "provided via OCI_IMAGE_ID"
    # Step 4 - Get Image ID of Compute Shape
    if not OCI_IMAGE_ID:
        images = execute_oci_command(
            compute_client,
            "list_images",
            compartment_id=oci_tenancy,
            shape=OCI_COMPUTE_SHAPE,
        )

        shortened_images = [
            {key: json.loads(str(image))[key] for key in IMAGE_LIST_KEYS}
            for image in images
        ]
        write_into_file(
            'images_list.json',
            json.dumps(shortened_images, indent=2)
        )

        # OCI reports Minimal aarch64 images with the OS version
        # "24.04 Minimal aarch64", while the dated image name contains
        # the actual release date.
        matching_images = [
            image for image in images
            if image.operating_system == OPERATING_SYSTEM
            and image.operating_system_version == "24.04 Minimal aarch64"
            and "Canonical-Ubuntu-24.04-Minimal-aarch64-" in image.display_name
        ]

        if not matching_images:
            raise ValueError(
                f"No matching OCI image found for "
                f"Canonical Ubuntu 24.04 Minimal aarch64. "
                f"Available images were written to images_list.json."
            )

        # Select the newest dated image returned by OCI.
        matching_images.sort(
            key=lambda image: (
                getattr(image, "time_created", None) or "",
                image.display_name,
            ),
            reverse=True,
        )

        selected_image = matching_images[0]
        oci_image_id = selected_image.id
        image_display_name = selected_image.display_name

        logging.info(
            "Selected newest OCI image: %s (%s)",
            image_display_name,
            oci_image_id,
        )
        print(
            f" Selected image: {image_display_name}",
            flush=True
        )
    else:
        oci_image_id = OCI_IMAGE_ID
        image_display_name = "OCI_IMAGE_ID override"

    assign_public_ip = ASSIGN_PUBLIC_IP.lower() in ["true", "1", "y", "yes"]
    boot_volume_size = max(50, int(BOOT_VOLUME_SIZE))
    ssh_public_key = read_or_generate_ssh_public_key(SSH_AUTHORIZED_KEYS_FILE)
    if not ssh_public_key:
        raise ValueError("No SSH public key is available. Create the key pair locally and provide SSH_AUTHORIZED_KEYS or SSH_AUTHORIZED_KEYS_FILE.")

    instance_exist_flag = check_instance_state_and_write(oci_tenancy, OCI_COMPUTE_SHAPE, tries=1)

    if OCI_COMPUTE_SHAPE == ARM_SHAPE:
        ocpus, memory_in_gbs = 2, 12
    else:
        ocpus, memory_in_gbs = 1, 1
    shape_config = oci.core.models.LaunchInstanceShapeConfigDetails(
        ocpus=ocpus, memory_in_gbs=memory_in_gbs
    )

    while not instance_exist_flag:
        availability_domain = next(oci_ad_names)
        logging.info(
            "=== OCI INSTANCE CONFIGURATION ===\n"
            "Shape: %s\nOCPUs: %s\nMemory: %s GB\n"
            "Operating System: %s\nOS Version: %s\n"
            "Image OCID: %s\nImage Name: %s\n"
            "Boot Volume: %s GB\nBoot Volume VPUs/GB: %s\n"
            "Availability Domain: %s\nSubnet: %s\nAssign Public IP: %s\n"
            "==================================",
            OCI_COMPUTE_SHAPE, ocpus, memory_in_gbs, OPERATING_SYSTEM, OS_VERSION,
            oci_image_id, image_display_name, boot_volume_size,
            boot_volume_vpus_per_gb, availability_domain, oci_subnet_id, assign_public_ip,
        )
        try:
            launch_instance_response = compute_client.launch_instance(
                launch_instance_details=oci.core.models.LaunchInstanceDetails(
                    availability_domain=availability_domain,
                    compartment_id=oci_tenancy,
                    create_vnic_details=oci.core.models.CreateVnicDetails(
                        assign_public_ip=assign_public_ip,
                        assign_private_dns_record=True,
                        display_name=DISPLAY_NAME,
                        subnet_id=oci_subnet_id,
                    ),
                    display_name=DISPLAY_NAME,
                    shape=OCI_COMPUTE_SHAPE,
                    availability_config=oci.core.models.LaunchInstanceAvailabilityConfigDetails(
                        recovery_action="RESTORE_INSTANCE"
                    ),
                    instance_options=oci.core.models.InstanceOptions(
                        are_legacy_imds_endpoints_disabled=False
                    ),
                    shape_config=shape_config,
                    source_details=oci.core.models.InstanceSourceViaImageDetails(
                        source_type="image",
                        image_id=oci_image_id,
                        boot_volume_size_in_gbs=boot_volume_size,
                        boot_volume_vpus_per_gb=boot_volume_vpus_per_gb,
                    ),
                    metadata={"ssh_authorized_keys": ssh_public_key},
                )
            )
            if launch_instance_response.status == 200:
                logging_step5.info(
                    "Command: launch_instance\nOutput: %s", launch_instance_response
                )
                instance_exist_flag = check_instance_state_and_write(oci_tenancy, OCI_COMPUTE_SHAPE)

        except oci.exceptions.ServiceError as srv_err:
            if srv_err.code == "LimitExceeded":
                logging_step5.info(
                    "Encountered LimitExceeded; checking whether an instance was created. "
                    "code=%s message=%s status=%s", srv_err.code, srv_err.message, srv_err.status)
                instance_exist_flag = check_instance_state_and_write(oci_tenancy, OCI_COMPUTE_SHAPE)
                if instance_exist_flag:
                    logging_step5.info("%s: instance creation confirmed; exiting successfully", srv_err.code)
                    return
                logging_step5.info("Didn't find an instance , proceeding with retries")
            data = {
                "status": srv_err.status,
                "code": srv_err.code,
                "message": srv_err.message,
            }
            handle_errors("launch_instance", data, logging_step5)
        except oci.exceptions.RequestException as req_err:
            data = {
                "status": None,
                "code": "RequestException",
                "message": str(req_err),
            }
            handle_errors("launch_instance", data, logging_step5)


if __name__ == "__main__":
    send_discord_message(" OCI Instance Creation Script: Starting up! Let's create some cloud magic!")
    try:
        launch_instance()
        send_discord_message(" Success! OCI Instance has been created. Time to celebrate!")
    except Exception as e:
        error_message = f" Oops! Something went wrong with the OCI Instance Creation Script:\n{str(e)}"
        send_discord_message(error_message)
        raise
