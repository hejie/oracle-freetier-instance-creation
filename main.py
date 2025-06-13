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
from typing import List, Dict, Any, Optional

import oci
import paramiko
import requests
from dotenv import load_dotenv
from oci.exceptions import ServiceError

# --- Constants ---
ARM_SHAPE = "VM.Standard.A1.Flex"
E2_MICRO_SHAPE = "VM.Standard.E2.1.Micro"
VALID_SHAPES = [ARM_SHAPE, E2_MICRO_SHAPE]

LOG_DIR = Path(__file__).parent
LOG_ERROR_FILE = LOG_DIR / "ERROR_IN_CONFIG.log"
LOG_INFO_FILE = LOG_DIR / "launch_instance.log"
INSTANCE_CREATED_FILE = LOG_DIR / "INSTANCE_CREATED"
UNHANDLED_ERROR_FILE = LOG_DIR / "UNHANDLED_ERROR.log"
IMAGES_LIST_FILE = LOG_DIR / "images_list.json"
EMAIL_TEMPLATE_FILE = LOG_DIR / "email_content.html"
OCI_ENV_FILE = Path.home() / "oci-dev/env/oci.env"


class Config:
    """Loads, validates, and stores all configuration for the script."""

    def __init__(self, env_path: Path):
        if not env_path.is_file():
            raise FileNotFoundError(f"Environment file not found at {env_path}")
        load_dotenv(env_path)

        # Load and validate configuration
        self.oci_config_path = self._get_env("OCI_CONFIG")
        self.oci_user_id = self._get_oci_user_id()
        self.free_ad_list = self._get_env("OCT_FREE_AD").split(",")
        self.display_name = self._get_env("DISPLAY_NAME")
        self.wait_time_secs = int(self._get_env("REQUEST_WAIT_TIME_SECS", "60"))
        self.ssh_keys_file = Path(self._get_env("SSH_AUTHORIZED_KEYS_FILE")).expanduser()
        self.image_id = self._get_env("OCI_IMAGE_ID", optional=True)
        self.compute_shape = self._get_env("OCI_COMPUTE_SHAPE", ARM_SHAPE)
        self.is_second_micro_instance = self._get_env("SECOND_MICRO_INSTANCE", "False").lower() == 'true'
        self.subnet_id = self._get_env("OCI_SUBNET_ID", optional=True)
        self.os = self._get_env("OPERATING_SYSTEM")
        self.os_version = self._get_env("OS_VERSION")
        self.assign_public_ip = self._get_env("ASSIGN_PUBLIC_IP", "false").lower() == "true"
        self.boot_volume_size = max(50, int(self._get_env("BOOT_VOLUME_SIZE", "50")))

        # Notification Settings
        self.notify_email = self._get_env("NOTIFY_EMAIL", "False").lower() == 'true'
        self.email_address = self._get_env("EMAIL", optional=not self.notify_email)
        self.email_password = self._get_env("EMAIL_PASSWORD", optional=not self.notify_email)

        self.notify_telegram = bool(self._get_env("TELEGRAM_POST", optional=True))
        self.telegram_post_url = self._get_env("TELEGRAM_POST", optional=True)
        self.telegram_user_id = self._get_env("TELEGRAM_USER_ID", optional=not self.notify_telegram)

        self._validate_config()

    def _get_env(self, key: str, default: str = None, optional: bool = False) -> str:
        value = os.getenv(key, default)
        if value is None and not optional:
            raise ValueError(f"Missing required environment variable: {key}")
        return value.strip() if value else value

    def _get_oci_user_id(self) -> str:
        parser = configparser.ConfigParser()
        try:
            parser.read(self.oci_config_path)
            # Validate for spaces in OCI config
            if any(' ' in value for section in parser.sections() for _, value in parser.items(section)):
                raise ValueError("oci_config file contains spaces in values, which is not acceptable.")
            return parser.get('DEFAULT', 'user')
        except (configparser.Error, FileNotFoundError) as e:
            raise ValueError(f"Error reading OCI config file at {self.oci_config_path}: {e}")

    def _validate_config(self):
        if self.compute_shape not in VALID_SHAPES:
            raise ValueError(f"'{self.compute_shape}' is not an acceptable shape. Use one of {VALID_SHAPES}")
        logging.info("Configuration loaded and validated successfully.")


class Notifier:
    """Handles sending notifications via Email and Telegram."""

    def __init__(self, config: Config):
        self.config = config

    def send(self, subject: str, text_body: str, html_body: Optional[str] = None):
        """Send a notification to all configured channels."""
        if self.config.notify_email:
            self._send_email(subject, html_body or text_body)
        if self.config.notify_telegram:
            # Telegram prefers simpler messages, often text is better
            self._send_telegram(text_body)

    def _send_email(self, subject: str, body: str):
        message = MIMEMultipart()
        message["Subject"] = subject
        message["From"] = self.config.email_address
        message["To"] = self.config.email_address
        message.attach(MIMEText(body, "html" if "<" in body else "plain"))

        try:
            with smtplib.SMTP("smtp.gmail.com", 587) as server:
                server.starttls()
                server.login(self.config.email_address, self.config.email_password)
                server.sendmail(self.config.email_address, self.config.email_address, message.as_string())
            logging.info("Email notification sent successfully.")
        except smtplib.SMTPException as e:
            logging.error(f"Failed to send email: {e}")

    def _send_telegram(self, message: str):
        payload = {"chat_id": self.config.telegram_user_id, "text": message}
        try:
            response = requests.post(self.config.telegram_post_url, json=payload, timeout=10)
            response.raise_for_status()
            logging.info("Telegram notification sent successfully.")
        except requests.RequestException as e:
            logging.error(f"Failed to send Telegram message: {e}")


class OciManager:
    """Manages all interactions with the OCI API."""

    def __init__(self, config: Config, notifier: Notifier):
        self.config = config
        self.notifier = notifier
        self.oci_sdk_config = oci.config.from_file(self.config.oci_config_path)
        self.iam_client = oci.identity.IdentityClient(self.oci_sdk_config)
        self.network_client = oci.core.VirtualNetworkClient(self.oci_sdk_config)
        self.compute_client = oci.core.ComputeClient(self.oci_sdk_config)
        self.tenancy_id = self._execute_api_call(self.iam_client, "get_user", self.config.oci_user_id).compartment_id

    def _execute_api_call(self, client, method, *args, **kwargs) -> Any:
        """A wrapper for OCI API calls with standardized error handling and retries."""
        while True:
            try:
                response = getattr(client, method)(*args, **kwargs)
                return response.data
            except ServiceError as e:
                # Temporary errors that should be retried
                if e.status in [429, 500, 502, 503, 504] or e.code == "TooManyRequests" or "Out of host capacity" in e.message:
                    logging.warning(f"Temporary OCI API error: {e.code} ({e.message}). Retrying in {self.config.wait_time_secs}s...")
                    time.sleep(self.config.wait_time_secs)
                    continue
                # For LimitExceeded, we want to stop trying to create, but check if an instance exists.
                elif e.code == "LimitExceeded":
                    logging.warning(f"Encountered LimitExceeded error. This usually means the free tier limit is reached.")
                    raise  # Re-raise to be handled by the calling function
                else:
                    # Permanent or unhandled error
                    logging.error(f"Unhandled OCI ServiceError: {e.status} {e.code} - {e.message}")
                    raise

    def find_availability_domains(self) -> List[str]:
        """Finds availability domains matching the configuration."""
        all_ads = self._execute_api_call(self.iam_client, "list_availability_domains", self.tenancy_id)
        return [ad.name for ad in all_ads if any(ad.name.endswith(free_ad) for free_ad in self.config.free_ad_list)]

    def find_subnet_id(self) -> str:
        """Finds a suitable subnet ID if not provided in config."""
        if self.config.subnet_id:
            return self.config.subnet_id
        subnets = self._execute_api_call(self.network_client, "list_subnets", compartment_id=self.tenancy_id)
        if not subnets:
            raise RuntimeError("No subnets found in the compartment. Please create one or specify OCI_SUBNET_ID.")
        return subnets[0].id

    def find_image_id(self) -> str:
        """Finds a suitable image ID based on OS and version if not provided."""
        if self.config.image_id:
            return self.config.image_id

        images = self._execute_api_call(self.compute_client, "list_images", compartment_id=self.tenancy_id, shape=self.config.compute_shape)
        
        # Log available images for user reference
        image_info = [{"display_name": i.display_name, "id": i.id, "os": i.operating_system, "os_version": i.operating_system_version} for i in images]
        with open(IMAGES_LIST_FILE, 'w') as f:
            json.dump(image_info, f, indent=2)

        for image in images:
            if image.operating_system == self.config.os and image.operating_system_version == self.config.os_version:
                return image.id
        
        raise RuntimeError(f"No image found for OS '{self.config.os}' version '{self.config.os_version}'. Check images_list.json for available options.")

    def check_for_existing_instance(self) -> Optional[oci.core.models.Instance]:
        """Checks if a target instance already exists and is in a running/provisioning state."""
        instances = self._execute_api_call(self.compute_client, "list_instances", compartment_id=self.tenancy_id)
        target_instances = [
            inst for inst in instances
            if inst.shape == self.config.compute_shape and inst.lifecycle_state in ["RUNNING", "PROVISIONING", "STARTING"]
        ]

        if self.config.compute_shape == ARM_SHAPE and target_instances:
            logging.info(f"Found existing ARM instance: {target_instances[0].display_name} ({target_instances[0].id})")
            return target_instances[0]
        
        if self.config.compute_shape == E2_MICRO_SHAPE:
            # We want the second one, so we succeed if we have 2 or more
            if self.config.is_second_micro_instance and len(target_instances) >= 2:
                logging.info(f"Found {len(target_instances)} micro instances. Target of 2 met.")
                return target_instances[-1]
            # We want the first one, so we succeed if we have 1 or more
            if not self.config.is_second_micro_instance and len(target_instances) >= 1:
                logging.info(f"Found existing Micro instance: {target_instances[0].display_name}")
                return target_instances[0]

        return None
    
    def _get_ssh_public_key(self) -> str:
        """Reads or generates an SSH public key."""
        key_file = self.config.ssh_keys_file
        if not key_file.is_file():
            logging.info(f"SSH key not found at {key_file}. Generating a new key pair.")
            key_file.parent.mkdir(parents=True, exist_ok=True)
            private_key_file = key_file.with_name(f"{key_file.stem}_private")
            
            key = paramiko.RSAKey.generate(4096)
            key.write_private_key_file(private_key_file)
            key_file.write_text(f"ssh-rsa {key.get_base64()} auto_generated_by_script")
            logging.info(f"Private key saved to: {private_key_file}")
            logging.info(f"Public key saved to: {key_file}")
        
        return key_file.read_text()

    def launch_new_instance(self):
        """The main loop to launch a compute instance."""
        logging.info("--- Starting Instance Launch Process ---")

        # Prepare instance details
        ads = self.find_availability_domains()
        if not ads:
            raise RuntimeError(f"No availability domains found matching patterns: {self.config.free_ad_list}")
        ad_cycler = itertools.cycle(ads)
        
        subnet_id = self.find_subnet_id()
        image_id = self.find_image_id()
        ssh_key = self._get_ssh_public_key()

        if self.config.compute_shape == ARM_SHAPE:
            shape_config = oci.core.models.LaunchInstanceShapeConfigDetails(ocpus=4, memory_in_gbs=24)
        else:
            shape_config = oci.core.models.LaunchInstanceShapeConfigDetails(ocpus=1, memory_in_gbs=1)

        launch_details = oci.core.models.LaunchInstanceDetails(
            compartment_id=self.tenancy_id,
            display_name=self.config.display_name,
            shape=self.config.compute_shape,
            subnet_id=subnet_id,
            image_id=image_id,
            availability_domain=next(ad_cycler), # Start with the first AD
            create_vnic_details=oci.core.models.CreateVnicDetails(
                assign_public_ip=self.config.assign_public_ip,
                assign_private_dns_record=True,
                display_name=self.config.display_name,
                subnet_id=subnet_id,
            ),
            shape_config=shape_config,
            source_details=oci.core.models.InstanceSourceViaImageDetails(
                source_type="image",
                image_id=image_id,
                boot_volume_size_in_gbs=self.config.boot_volume_size,
            ),
            metadata={"ssh_authorized_keys": ssh_key},
        )

        # Loop until instance is created or a permanent error occurs
        while True:
            try:
                # Cycle through availability domains
                launch_details.availability_domain = next(ad_cycler)
                logging.info(f"Attempting to launch instance in AD: {launch_details.availability_domain}")
                
                self.compute_client.launch_instance(launch_details) # Note: not using the wrapper here to handle status codes
                
                logging.info(f"Launch command sent successfully for AD {launch_details.availability_domain}. Verifying instance state...")
                # After successful launch command, poll for a few minutes to confirm creation
                for _ in range(5): # Poll for up to 5 minutes
                    instance = self.check_for_existing_instance()
                    if instance:
                        logging.info("Instance confirmed to be in a running/provisioning state.")
                        return instance # Success!
                    time.sleep(60)
                
                logging.error("Launch command was sent, but instance did not appear in running state after 5 minutes.")
                raise RuntimeError("Instance verification failed.")

            except ServiceError as e:
                if e.code == "LimitExceeded":
                    logging.warning("LimitExceeded error received. Checking if an instance was created just before the limit was hit.")
                    instance = self.check_for_existing_instance()
                    if instance:
                        logging.info("An instance already exists, likely the cause of the LimitExceeded error. Exiting.")
                        return instance # Success, an instance exists.
                    # If no instance, the limit is for some other resource.
                    raise RuntimeError("LimitExceeded for a resource other than the target instance shape. Manual intervention required.")
                
                # Let the retry logic handle temporary capacity errors
                if e.status in [429, 500, 502, 503, 504] or e.code == "TooManyRequests" or "Out of host capacity" in e.message:
                    logging.warning(f"status:{e.status} code:{e.code} message:{e.message} Trying next AD or retrying in {self.config.wait_time_secs}s...")
                    time.sleep(self.config.wait_time_secs)
                    continue # Loop will try the next AD or retry
                
                # For any other service error, it's unhandled.
                logging.error(f"Unhandled ServiceError during launch: {e}")
                raise

def setup_logging():
    """Configures logging for the script."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(LOG_INFO_FILE),
            logging.StreamHandler(sys.stdout) # Also print to console
        ]
    )
    # Specific logger for launch attempts
    # launch_logger = logging.getLogger("launch_instance")
    # fh = logging.FileHandler(LOG_LAUNCH_FILE)
    # fh.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    # launch_logger.addHandler(fh)

def create_instance_success_files(instance: oci.core.models.Instance) -> str:
    """Creates the INSTANCE_CREATED file and generates a success message."""
    details = (
        f"Instance successfully created or found!\n\n"
        f"ID: {instance.id}\n"
        f"Display Name: {instance.display_name}\n"
        f"Availability Domain: {instance.availability_domain}\n"
        f"Shape: {instance.shape}\n"
        f"State: {instance.lifecycle_state}\n"
    )
    INSTANCE_CREATED_FILE.write_text(details)
    
    html_body = f"<h1>Instance Created!</h1><p>{details.replace(chr(10),'<br>')}</p>"
    if EMAIL_TEMPLATE_FILE.is_file():
        html_template = EMAIL_TEMPLATE_FILE.read_text()
        html_body = html_template.replace('<INSTANCE_ID>', instance.id)
        html_body = html_body.replace('<DISPLAY_NAME>', instance.display_name)
        html_body = html_body.replace('<AD>', instance.availability_domain)
        html_body = html_body.replace('<SHAPE>', instance.shape)
        html_body = html_body.replace('<STATE>', instance.lifecycle_state)
    
    return details, html_body

def main():
    """Main execution function."""
    notifier = None
    try:
        # 1. Load and Validate Configuration
        config = Config(env_path=OCI_ENV_FILE)
        notifier = Notifier(config)
        notifier.send("OCI Script Starting", "🚀 OCI Instance Creation Script: Starting up! Let's create some cloud magic!")

        # 2. Initialize OCI Manager
        oci_manager = OciManager(config, notifier)

        # 3. Check if an instance already exists
        instance = oci_manager.check_for_existing_instance()
        if instance:
            logging.info("An existing instance that meets the criteria was found.")
            text_body, html_body = create_instance_success_files(instance)
            notifier.send("OCI Instance Found", text_body, html_body)
            sys.exit(0)

        # 4. If not, launch a new one
        logging.info("No existing instance found. Proceeding to launch a new one.")
        instance = oci_manager.launch_new_instance()

        # 5. Success
        if instance:
            text_body, html_body = create_instance_success_files(instance)
            notifier.send("🎉 OCI Instance Created!", text_body, html_body)
        else:
            # This case should ideally not be reached due to error handling inside launch
            raise RuntimeError("Launch process finished without creating an instance and without an error.")

    except (ValueError, FileNotFoundError, RuntimeError) as e:
        # Controlled, known errors (config issues, etc.)
        logging.error(f"A configuration or runtime error occurred: {e}")
        LOG_ERROR_FILE.write_text(str(e))
        if notifier:
            notifier.send("OCI Script Failed (Config/Runtime Error)", f"😕 The script failed with a known error:\n\n{e}")
        sys.exit(1)
    except Exception as e:
        # Unhandled exceptions
        logging.exception("An unhandled exception occurred!")
        error_message = (
            f"😱 Yikes! The script encountered an unhandled error and exited unexpectedly.\n\n"
            f"ERROR TYPE: {type(e).__name__}\n"
            f"ERROR DETAILS: {e}\n\n"
            "Please check the logs and consider raising an issue on GitHub."
        )
        UNHANDLED_ERROR_FILE.write_text(error_message)
        if notifier:
            notifier.send("🔥 OCI Script CRASHED!", error_message)
        sys.exit(1)


if __name__ == "__main__":
    setup_logging()
    main()