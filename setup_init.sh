#!/usr/bin/env bash

# --- Script Configuration and Best Practices ---

# Exit immediately if a command exits with a non-zero status.
set -e
# Treat unset variables as an error when substituting.
set -u
# Pipelines return the exit status of the last command to exit with a non-zero status,
# or zero if no command exited with a non-zero status.
set -o pipefail

# --- Global Variables ---
VENV_DIR=".venv"
PYTHON_SCRIPT="main.py"
LOG_ERROR="ERROR_IN_CONFIG.log"
LOG_SUCCESS="launch_instance.log"
LOG_INSTANCE_CREATED="INSTANCE_CREATED"
OCI_ENV_FILE=~/oci-dev/env/oci.env

# Making environment non-interactive for package installations
export DEBIAN_FRONTEND=noninteractive

# --- Notification Functions (Unchanged) ---

# Function to send Discord message
send_discord_message() {
    # It's safer to pass the message as a JSON-escaped string
    local message_json
    message_json=$(jq -n --arg content "$1" '{content: $content}')
    curl -s -H "Content-Type: application/json" -X POST -d "$message_json" "$DISCORD_WEBHOOK"
}

# Function to send Telegram message
send_telegram_message() {
    curl -s -X POST "https://api.telegram.org/bot$TELEGRAM_TOKEN/sendMessage" \
         -d chat_id="$TELEGRAM_USER_ID" \
         -d text="$1"
}

# General interface to send notifications
send_notification() {
  # Check all channels
  if [[ -n "${DISCORD_WEBHOOK:-}" ]]; then
      send_discord_message "$1" & # Send in background to not block script
  fi

  if [[ -n "${TELEGRAM_TOKEN:-}" && -n "${TELEGRAM_USER_ID:-}" ]]; then
      send_telegram_message "$1" & # Send in background
  fi
}

# --- Signal Handling Functions ---

# This function is called when the script is interrupted (Ctrl+C) or terminated.
script_interrupted() {
    echo -e "\n🛑 Script interrupted. Sending notification and cleaning up..."
    # $SCRIPT_PID might not be set if interruption happens early
    if [[ -n "${SCRIPT_PID:-}" && -e /proc/$SCRIPT_PID ]]; then
        kill "$SCRIPT_PID"
    fi
    send_notification "🛑 Heads up! The OCI Instance Creation Script has been interrupted or stopped."
    # The 'deactivate' command might not be available if the venv wasn't activated
    type deactivate >/dev/null 2>&1 && deactivate
    exit 1
}

# Function to handle suspension (Ctrl+Z)
handle_suspend() {
    echo -e "\n⏸️ Script suspended. To resume, use 'fg' command."
    send_notification "⏸️ The OCI Instance Creation Script has been suspended."
    # $SCRIPT_PID might not be set yet
    if [[ -n "${SCRIPT_PID:-}" && -e /proc/$SCRIPT_PID ]]; then
        kill -STOP "$SCRIPT_PID"
    fi
    # This stops the script itself. When resumed with `fg`, it continues from here.
    kill -STOP $$
}


# --- Core Logic Functions ---

# Sets up the Python virtual environment and installs dependencies.
setup_environment() {
    echo "--- Setting up environment ---"
    # The 'rerun' argument is now for forcing a full reinstall.
    # Otherwise, we check if the venv exists and skip setup.
    if [[ "$1" == "rerun" || ! -d "$VENV_DIR" ]]; then
        echo "Creating/recreating Python environment..."
        if type apt >/dev/null 2>&1; then
            echo "Updating package lists and installing required packages..."
            sudo apt-get update -y
            sudo apt-get install -y python3-venv python3-pip jq # jq is useful for handling json
        fi
        rm -rf "$VENV_DIR" # Clean up old venv if forcing rerun
        python3 -m venv "$VENV_DIR"

        # Activate and install packages inside this function's scope
        source "${VENV_DIR}/bin/activate"
        
        echo "Upgrading pip and installing requirements..."
        pip install --upgrade pip
        pip install wheel setuptools
        pip install -r requirements.txt
        
        # Deactivate so the main script can activate it later.
        # This keeps activation scope clean.
        deactivate
        echo "Environment setup complete."
    else
        echo "Virtual environment already exists. Skipping setup."
        echo "Use './setup_init.sh rerun' to force a reinstall."
    fi
}

# Waits for the Python script to produce an initial status log file.
# This replaces the unreliable `sleep` calls.
wait_for_initial_status() {
    local timeout=120 # 2 minutes timeout
    local interval=5  # check every 5 seconds
    local end_time=$((SECONDS + timeout))

    echo "Waiting for script's initial status (max ${timeout}s)..."

    while [[ $SECONDS -lt $end_time ]]; do
        if [[ -s "$LOG_ERROR" ]]; then
            echo "Error detected in config. Check '$LOG_ERROR'."
            send_notification "😕 Uh-oh! There's an error in the config. Check $LOG_ERROR and give it another shot!"
            return 1 # Indicate failure
        elif [[ -s "$LOG_INSTANCE_CREATED" ]]; then
            echo "Instance created or limit reached. Check '$LOG_INSTANCE_CREATED'."
            send_notification "🎊 Great news! An instance was created or we've hit the Free tier limit. Check the '$LOG_INSTANCE_CREATED' file for details!"
            return 0 # Indicate success
        elif [[ -s "$LOG_SUCCESS" ]]; then
            echo "Script is running successfully."
            send_notification "👍 All systems go! The script is running smoothly."
            return 0 # Indicate success
        fi
        sleep "$interval"
    done

    echo "Timeout reached. No initial status log file found."
    send_notification "😱 Yikes! The script didn't start correctly or create a log file in time. Please check for errors."
    return 1 # Indicate failure
}

# Monitors the background Python process until it finishes.
monitor_process() {
    local pid_to_watch=$1
    echo "--- Monitoring Python script (PID: ${pid_to_watch}) ---"
    echo "You can safely close this terminal. The script will continue running."

    # Check every 60 seconds if the process is still running.
    while ps -p "$pid_to_watch" > /dev/null; do
        sleep 60
    done

    echo "Python script (PID: ${pid_to_watch}) has finished."
}

# --- Main Execution ---

main() {
    # Set traps to catch signals
    trap script_interrupted SIGINT SIGTERM
    trap handle_suspend SIGTSTP

    echo "--- OCI Instance Creation Script ---"
    
    # 1. Clean up previous log files
    echo "Deleting previous log files..."
    rm -f *.log INSTANCE_CREATED

    # 2. Set up the environment if needed
    setup_environment "${1:-}" # Pass first argument ('rerun' or empty)

    # 3. Activate virtual environment
    echo "Activating virtual environment..."
    # shellcheck source=/dev/null
    source "${VENV_DIR}/bin/activate"

    # 4. Load OCI environment variables
    if [[ ! -f "$OCI_ENV_FILE" ]]; then
        echo "Error: OCI environment file not found at '$OCI_ENV_FILE'"
        send_notification "🔥 Critical Error: OCI environment file not found at '$OCI_ENV_FILE'. Script cannot continue."
        exit 1
    fi
    echo "Loading OCI environment variables..."
    # shellcheck source=/dev/null
    source "$OCI_ENV_FILE"

    # 5. Run the Python program in the background
    echo "Starting Python script '$PYTHON_SCRIPT' in the background..."
    nohup python3 "$PYTHON_SCRIPT" >/dev/null 2>&1 &
    SCRIPT_PID=$!
    # Exporting PID so traps can access it
    export SCRIPT_PID

    # 6. Wait for initial status and handle result
    if ! wait_for_initial_status; then
        echo "Script initialization failed. Exiting."
        # If the script failed to start, kill the lingering process
        kill "$SCRIPT_PID" 2>/dev/null || true
        script_interrupted # Send failure notification
        exit 1
    fi
    
    # 7. Monitor the process until it completes
    monitor_process "$SCRIPT_PID"

    # 8. Final Notification and Cleanup
    echo "--- Script finished ---"
    send_notification "🏁 The OCI Instance Creation Script has finished its run."
    
    deactivate
    echo "Virtual environment deactivated. Exiting."
    exit 0
}

# Run the main function, passing all script arguments to it.
main "$@"