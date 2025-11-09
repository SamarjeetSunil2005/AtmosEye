import subprocess
import logging
import re
import time
import os
import sys
from typing import List, Dict, Any, Optional

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
WLAN_UPLINK = "wlan0"
WLAN_EDITABLE = "wlan1"
WPA_SOCKET_PATH = "/var/run/wpa_supplicant"

def _run_command(cmd: List[str], use_sudo: bool = False, timeout: int = 15) -> Optional[str]:
    if use_sudo:
        cmd.insert(0, 'sudo')
    try:
        effective_timeout = 20 if 'iwlist' in cmd else timeout
        logging.debug(f"Running command: {' '.join(cmd)} (Timeout: {effective_timeout}s)")
        result = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=effective_timeout, errors='replace')
        return result.stdout.strip()
    except FileNotFoundError:
        logging.error(f"Command not found: {cmd[0]}. Is the tool installed?")
        return None
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.strip() if e.stderr else ""
        is_wpa_cli_check = len(cmd) > 1 and cmd[1] == 'wpa_cli' and ("No such file or directory" in stderr or "Could not connect" in stderr)
        if not is_wpa_cli_check:
             logging.error(f"Command failed: {' '.join(cmd)}\nError: {stderr}")
        return None
    except subprocess.TimeoutExpired:
        logging.error(f"Command timed out: {' '.join(cmd)}")
        return None
    except Exception as e:
        logging.error(f"Unexpected error running command {' '.join(cmd)}: {e}", exc_info=True)
        return None

def _wpa_cli_command(interface: str, *args: str) -> List[str]:
    return ['wpa_cli', '-p', WPA_SOCKET_PATH, '-i', interface] + list(args)

def _wait_for_socket(interface: str, timeout: int = 10) -> bool:
    service_name = f'wpa_supplicant@{interface}.service'
    socket_file_path = os.path.join(WPA_SOCKET_PATH, interface)
    conf_file = f"/etc/wpa_supplicant/wpa_supplicant-{interface}.conf"
    required_ctrl_line = f"ctrl_interface=DIR={WPA_SOCKET_PATH} GROUP=netdev"

    if not os.path.exists(WPA_SOCKET_PATH):
        try:
            logging.info(f"Socket directory {WPA_SOCKET_PATH} not found. Creating it.")
            _run_command(['mkdir', '-p', WPA_SOCKET_PATH], use_sudo=True)
            _run_command(['chown', 'root:netdev', WPA_SOCKET_PATH], use_sudo=True)
            _run_command(['chmod', '750', WPA_SOCKET_PATH], use_sudo=True)
        except Exception as e:
            logging.error(f"Failed to create socket directory {WPA_SOCKET_PATH}: {e}")
            return False
    else:
        try:
            _run_command(['chown', 'root:netdev', WPA_SOCKET_PATH], use_sudo=True)
            _run_command(['chmod', '750', WPA_SOCKET_PATH], use_sudo=True)
        except Exception as e:
             logging.warning(f"Could not set permissions on existing socket directory {WPA_SOCKET_PATH}: {e}")

    logging.debug(f"Ensuring interface {interface} is up and Wi-Fi is unblocked.")
    _run_command(['ip', 'link', 'set', interface, 'up'], use_sudo=True)
    _run_command(['rfkill', 'unblock', 'wifi'], use_sudo=True)
    time.sleep(0.5)

    is_active = False
    for _ in range(3):
        if _run_command(['systemctl', 'is-active', service_name], use_sudo=True) == 'active':
            is_active = True
            break
        time.sleep(0.5)

    if not is_active:
        logging.warning(f"{service_name} is not active. Validating config and attempting restart...")

        if os.path.exists(socket_file_path):
             logging.warning(f"Stale socket file found at {socket_file_path} while service is down. Removing it.")
             _run_command(['rm', '-f', socket_file_path], use_sudo=True)

        try:
            config_content = ""
            config_needs_update = False
            if os.path.exists(conf_file):
                config_content_read = _run_command(['cat', conf_file], use_sudo=True)
                if config_content_read is None:
                     logging.error(f"Failed to read existing config file {conf_file} even with sudo. Will attempt to overwrite.")
                     config_needs_update = True
                     config_content = ""
                else:
                    config_content = config_content_read

            if required_ctrl_line not in config_content:
                logging.warning(f"Required line '{required_ctrl_line}' missing or incorrect in {conf_file}. Fixing it.")
                config_needs_update = True
                new_config_lines = [required_ctrl_line]
                if "update_config=1" not in config_content:
                    new_config_lines.append("update_config=1")
                
                network_blocks = re.findall(r'^\s*network\s*=\s*\{(?:[^{}]|\{(?:[^{}]|\{[^{}]*\})*\})*?^\s*\}', config_content, re.MULTILINE | re.DOTALL)
                new_config_lines.extend(network_blocks)
                config_content = "\n".join(new_config_lines) + "\n"
            
            elif "update_config=1" not in config_content:
                 logging.warning(f"'update_config=1' missing in {conf_file}. Adding it.")
                 config_needs_update = True
                 config_content = config_content.replace(required_ctrl_line, f"{required_ctrl_line}\nupdate_config=1", 1)

            
            if config_needs_update or not os.path.exists(conf_file):
                logging.info(f"Writing updated/new config file: {conf_file}")
                escaped_content = config_content.replace("'", "'\\''")
                write_cmd = ['bash', '-c', f"echo '{escaped_content}' | sudo tee {conf_file} > /dev/null"]
                if _run_command(write_cmd, use_sudo=False) is None:
                     raise IOError("Failed to write config file using tee.")
            else:
                 logging.debug(f"Config file {conf_file} seems okay.")

            _run_command(['chmod', '644', conf_file], use_sudo=True)
            _run_command(['chown', 'root:root', conf_file], use_sudo=True)
            
        except Exception as e:
            logging.error(f"CRITICAL: Failed during config file validation/creation for {conf_file}: {e}", exc_info=True)
            return False

        logging.info(f"Attempting to unmask, enable, and restart {service_name}...")
        _run_command(['systemctl', 'unmask', service_name], use_sudo=True)
        _run_command(['systemctl', 'enable', service_name], use_sudo=True)
        _run_command(['systemctl', 'restart', service_name], use_sudo=True)
        time.sleep(2)

        if _run_command(['systemctl', 'is-active', service_name], use_sudo=True) != 'active':
            logging.error(f"CRITICAL: {service_name} FAILED TO START after restart attempt.")
            status_log = _run_command(['systemctl', 'status', service_name, '--no-pager', '-l'], use_sudo=True)
            journal_log = _run_command(['journalctl', '-u', service_name, '-n', '20', '--no-pager'], use_sudo=True)
            logging.error(f"--- Service Status Log (systemctl status) ---\n{status_log}\n--- End of Status Log ---")
            logging.error(f"--- Service Journal Log (journalctl) ---\n{journal_log}\n--- End of Journal Log ---")
            return False
        else:
             logging.info(f"{service_name} successfully started.")

    start_time = time.time()
    socket_found = False
    while time.time() - start_time < timeout:
        if os.path.exists(socket_file_path):
            try:
                _run_command(['chmod', 'g+w', socket_file_path], use_sudo=True)
                logging.debug(f"Socket file {socket_file_path} found and permissions checked.")
                socket_found = True
                break
            except Exception as e:
                 logging.warning(f"Found socket file {socket_file_path} but failed to set permissions: {e}. Retrying check.")
        logging.warning(f"Waiting for socket file {socket_file_path} (service active)... Attempt {int(time.time() - start_time)+1}/{timeout}")
        time.sleep(0.5)

    if not socket_found:
        logging.error(f"Timeout waiting for socket file {socket_file_path}. Service is active, but socket did not appear or permissions failed.")
        return False

    return True

def get_interface_status(interface: str) -> Dict[str, Any]:
    status = {"state": "disconnected", "ssid": None, "ip_address": None, "signal_percent": 0, "mode": "unknown"}
    iwconfig_out = _run_command(['iwconfig', interface])
    if iwconfig_out:
        mode_match = re.search(r'Mode:(\w+)', iwconfig_out)
        if mode_match:
            mode = mode_match.group(1).lower()
            if mode == "managed": status["mode"] = "client"
            elif mode == "master": status["mode"] = "ap"
            elif mode == "monitor": status["mode"] = "monitor"
    
    ip_out = _run_command(['ip', 'addr', 'show', interface])
    if ip_out:
        ip_match = re.search(r'inet (\d+\.\d+\.\d+\.\d+)', ip_out)
        if ip_match: status["ip_address"] = ip_match.group(1)

    if status["mode"] == "client":
        if _wait_for_socket(interface, timeout=2):
            cli_status_raw = _run_command(_wpa_cli_command(interface, 'status'))
            if cli_status_raw:
                ssid_match = re.search(r'^ssid=(.*)$', cli_status_raw, re.MULTILINE)
                state_match = re.search(r'^wpa_state=(.*)$', cli_status_raw, re.MULTILINE)
                if ssid_match and state_match and state_match.group(1) == 'COMPLETED':
                    status["state"] = "connected"
                    status["ssid"] = ssid_match.group(1)
        
        if iwconfig_out:
            if status["state"] != "connected":
                 essid_match = re.search(r'ESSID:"([^"]+)"', iwconfig_out)
                 if essid_match and essid_match.group(1) != "off/any":
                      status["ssid"] = essid_match.group(1)
                      status["state"] = "associating"
                      
            q = re.search(r'Link Quality=(\d+)/(\d+)', iwconfig_out)
            level = re.search(r'Signal level=(-?\d+)\s+dBm', iwconfig_out)
            if q:
                try:
                    quality, total = int(q.group(1)), int(q.group(2))
                    status["signal_percent"] = round((quality / total) * 100)
                except Exception: pass
            elif level:
                 dbm = int(level.group(1))
                 if dbm >= -50: status["signal_percent"] = 100
                 elif dbm >= -60: status["signal_percent"] = 80
                 elif dbm >= -67: status["signal_percent"] = 60
                 elif dbm >= -70: status["signal_percent"] = 40
                 elif dbm >= -80: status["signal_percent"] = 20
                 else: status["signal_percent"] = 10


    elif status["mode"] == "ap":
         status["state"] = "active"
         if iwconfig_out:
             ssid_match = re.search(r'ESSID:"([^"]+)"', iwconfig_out)
             if ssid_match: status["ssid"] = ssid_match.group(1)

    logging.debug(f"Status for {interface}: {status}")
    return status

def get_status() -> Dict[str, Any]:
    wlan0_stat = get_interface_status(WLAN_UPLINK)
    wlan1_stat = get_interface_status(WLAN_EDITABLE)
    return {"wlan0": wlan0_stat, "wlan1": wlan1_stat}

def list_connected_devices(interface: str = WLAN_EDITABLE) -> List[Dict[str, Any]]:
    current_status = get_interface_status(interface)
    if current_status.get("mode") != "ap":
        logging.warning(f"Cannot list connected devices for {interface}, it's not in AP mode.")
        return []

    logging.info(f"Listing devices connected to AP on {interface}...")
    
    out = _run_command(['iw', 'dev', interface, 'station', 'dump'], use_sudo=True)
    devices = []
    mac_set = set()
    if out:
        for block in re.split(r'\n(?=Station )', out):
            mac_match = re.search(r'Station\s+([0-9a-fA-F:]{17})', block)
            if not mac_match: continue
            mac = mac_match.group(1).lower()
            if mac in mac_set: continue

            rx_bytes = re.search(r'rx bytes:\s*(\d+)', block)
            tx_bytes = re.search(r'tx bytes:\s*(\d+)', block)
            signal = re.search(r'signal:\s*(-?\d+)\s*dBm', block)
            inactive_time = re.search(r'inactive time:\s*(\d+)\s*ms', block)
            
            device_info = {"mac": mac}
            if rx_bytes: device_info["rx_bytes"] = int(rx_bytes.group(1))
            if tx_bytes: device_info["tx_bytes"] = int(tx_bytes.group(1))
            if signal: device_info["signal_dbm"] = int(signal.group(1))
            if inactive_time: device_info["inactive_ms"] = int(inactive_time.group(1))
            
            devices.append(device_info)
            mac_set.add(mac)
            
        logging.info(f"Found {len(devices)} stations via 'iw dev station dump'.")
        arp_out = _run_command(['arp', '-n'])
        ip_map = {}
        if arp_out:
            for line in arp_out.splitlines()[1:]:
                parts = line.split()
                if len(parts) >= 3 and re.match(r'([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}', parts[2]):
                    ip_map[parts[2].lower()] = parts[0]
        
        for dev in devices:
            dev["ip"] = ip_map.get(dev["mac"])

        return devices

    logging.warning("'iw dev station dump' failed or returned no devices. Falling back to ARP table.")
    arp_out = _run_command(['arp', '-n'])
    if arp_out:
        for line in arp_out.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 4 and parts[3] == interface and re.match(r'([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}', parts[2]):
                mac = parts[2].lower()
                if mac not in mac_set:
                    devices.append({"ip": parts[0], "mac": mac})
                    mac_set.add(mac)
        logging.info(f"Found {len(devices)} devices via ARP fallback.")
    
    return devices
    
def set_mode_wlan1(mode: str) -> Dict[str, Any]:
    if mode not in ('ap', 'client'):
        return {"error": True, "message": "Invalid mode. Use 'ap' or 'client'."}

    current_status = get_interface_status(WLAN_EDITABLE)
    if current_status.get("mode") == mode:
        return {"status": "success", "message": f"wlan1 is already in {mode} mode."}

    if mode == 'ap':
        service_to_stop = f'wpa_supplicant@{WLAN_EDITABLE}.service'
        service_to_start = 'hostapd.service'
        required_config_file = "/etc/hostapd/hostapd.conf"
    else:
        service_to_stop = 'hostapd.service'
        service_to_start = f'wpa_supplicant@{WLAN_EDITABLE}.service'

    if service_to_start == 'hostapd.service' and not os.path.exists(required_config_file):
         logging.error(f"Cannot switch to AP mode: {required_config_file} is missing.")
         return {"error": True, "message": f"hostapd is not configured. Cannot start AP mode."}

    logging.info(f"Attempting to switch {WLAN_EDITABLE} to {mode} mode.")
    logging.info(f"Stopping {service_to_stop}...")
    _run_command(['systemctl', 'stop', service_to_stop], use_sudo=True)
    time.sleep(1)

    logging.info(f"Resetting interface {WLAN_EDITABLE} state...")
    _run_command(['ip', 'link', 'set', WLAN_EDITABLE, 'down'], use_sudo=True)
    time.sleep(1)
    _run_command(['ip', 'link', 'set', WLAN_EDITABLE, 'up'], use_sudo=True)
    time.sleep(1)

    logging.info(f"Starting {service_to_start}...")
    _run_command(['systemctl', 'restart', service_to_start], use_sudo=True)
    time.sleep(3)

    status_output = _run_command(['systemctl', 'is-active', service_to_start], use_sudo=True)
    if status_output == 'active':
        logging.info(f"{service_to_start} is now active.")
        
        if mode == 'client':
            logging.info("Checking for wpa_supplicant socket availability...")
            if _wait_for_socket(WLAN_EDITABLE, timeout=15):
                logging.info("Socket found. Triggering reconfigure to connect to saved networks...")
                _run_command(_wpa_cli_command(WLAN_EDITABLE, 'reconfigure'))
            else:
                logging.error(f"Socket for {WLAN_EDITABLE} not found after switching to client mode. Connection may fail.")
                
        return {"status": "success", "message": f"wlan1 mode switched to {mode}."}
    else:
        logging.error(f"CRITICAL: FAILED TO START {service_to_start} after mode switch.")
        status_log = _run_command(['systemctl', 'status', service_to_start, '--no-pager', '-l'], use_sudo=True)
        journal_log = _run_command(['journalctl', '-u', service_to_start, '-n', '20', '--no-pager'], use_sudo=True)
        logging.error(f"--- Service Status Log ---\n{status_log}\n--- End of Status Log ---")
        logging.error(f"--- Service Journal Log ---\n{journal_log}\n--- End of Journal Log ---")

        logging.warning(f"Attempting to revert to previous network service: {service_to_stop}")
        _run_command(['systemctl', 'restart', service_to_stop], use_sudo=True)
        
        return {"error": True, "message": f"Failed to switch to {mode} mode. Check service logs for errors."}

def scan_networks(interface: str = WLAN_EDITABLE) -> List[Dict[str, Any]]:
    logging.debug(f"Ensuring interface {interface} is up before scanning.")
    _run_command(['ip', 'link', 'set', interface, 'up'], use_sudo=True)
    time.sleep(1)

    logging.info(f"Scanning for networks on {interface}...")
    output = _run_command(['iwlist', interface, 'scan'], use_sudo=True)

    if output:
        logging.debug(f"--- iwlist scan raw output for {interface} ---\n{output}\n--- End of raw output ---")
    else:
        logging.error(f"Scan on {interface} failed or returned NO results.")
        link_show = _run_command(['ip', 'link', 'show', interface])
        if not link_show:
             logging.error(f"Interface {interface} does not exist.")
        elif 'state DOWN' in link_show:
             logging.error(f"Interface {interface} is down.")
        rfkill_status = _run_command(['rfkill', 'list', 'wifi'], use_sudo=True)
        if rfkill_status and 'blocked' in rfkill_status.lower():
            logging.error(f"WiFi may be blocked by rfkill: {rfkill_status}")
        return []

    networks = []
    seen_ssids = set()

    cell_pattern = re.compile(r"""
        ^\s*Cell\s+\d+\s+-\s+Address:\s+([0-9A-Fa-f:]{17})
        .*?
        """, re.MULTILINE | re.DOTALL | re.VERBOSE)

    essid_pattern = re.compile(r'^\s*ESSID:"((?:[^"\\]|\\.)*)"', re.MULTILINE)
    quality_pattern = re.compile(r'^\s*Quality=(\d+)/(\d+)\s+Signal level=(-?\d+)\s+dBm', re.MULTILINE)
    encryption_pattern = re.compile(r'^\s*Encryption key:(on|off)', re.MULTILINE)
    wpa_pattern = re.compile(r'^\s*IE:\s*(?:IEEE\s+802\.11i/)?(WPA[23]?|RSN)\s+Version\s+\d+', re.MULTILINE)
    wps_pattern = re.compile(r'^\s*WPS:\s*State:', re.MULTILINE)

    output_lines = output.splitlines()
    current_cell_block = ""
    cell_address = None

    for line in output_lines:
        new_cell_match = re.match(r'^\s*Cell \d+ - Address: ([0-9A-Fa-f:]{17})', line)
        if new_cell_match:
            if current_cell_block and cell_address:
                essid_match = essid_pattern.search(current_cell_block)
                if essid_match:
                    try:
                        ssid = essid_match.group(1).encode('utf-8').decode('unicode_escape', 'replace')
                    except Exception:
                         ssid = essid_match.group(1)

                    if ssid and ssid not in seen_ssids:
                        seen_ssids.add(ssid)
                        signal_percent = 0
                        signal_dbm = None
                        quality_match = quality_pattern.search(current_cell_block)
                        if quality_match:
                            try:
                                quality, total, dbm_val = map(int, quality_match.groups())
                                signal_percent = round((quality / total) * 100) if total > 0 else 0
                                signal_dbm = dbm_val
                            except Exception as e:
                                logging.warning(f"Could not parse quality for SSID '{ssid}' (MAC: {cell_address}): {e}")
                        else:
                             level_match = re.search(r'^\s*Signal level=(-?\d+)\s+dBm', current_cell_block, re.MULTILINE)
                             if level_match:
                                 try:
                                     signal_dbm = int(level_match.group(1))
                                     if signal_dbm >= -50: signal_percent = 100
                                     elif signal_dbm >= -60: signal_percent = 80
                                     elif signal_dbm >= -67: signal_percent = 60
                                     elif signal_dbm >= -70: signal_percent = 40
                                     elif signal_dbm >= -80: signal_percent = 20
                                     else: signal_percent = 10
                                 except Exception: pass
                             else:
                                  logging.warning(f"Could not find Quality or Signal level for SSID '{ssid}' (MAC: {cell_address})")
                        
                        security_type = "Open"
                        encryption_match = encryption_pattern.search(current_cell_block)
                        if encryption_match and encryption_match.group(1) == 'on':
                            security_type = "WEP"
                            wpa_match = wpa_pattern.search(current_cell_block)
                            if wpa_match:
                                wpa_type = wpa_match.group(1)
                                if wpa_type == "RSN": security_type = "WPA2/WPA3"
                                else: security_type = wpa_type
                        
                        has_wps = bool(wps_pattern.search(current_cell_block))

                        networks.append({
                            'ssid': ssid,
                            'signal': signal_percent,
                            'signal_dbm': signal_dbm,
                            'security': security_type,
                            'wps': has_wps,
                            'mac': cell_address
                        })
                else:
                    logging.warning(f"Found Wi-Fi cell (MAC: {cell_address}) but could not parse ESSID.")

            current_cell_block = line + "\n"
            cell_address = new_cell_match.group(1)
        elif cell_address:
            current_cell_block += line + "\n"

    if current_cell_block and cell_address:
         essid_match = essid_pattern.search(current_cell_block)
         if essid_match:
            try:
                ssid = essid_match.group(1).encode('utf-8').decode('unicode_escape', 'replace')
            except Exception:
                ssid = essid_match.group(1)

            if ssid and ssid not in seen_ssids:
                signal_percent = 0
                signal_dbm = None
                quality_match = quality_pattern.search(current_cell_block)
                if quality_match:
                    try:
                        quality, total, dbm_val = map(int, quality_match.groups())
                        signal_percent = round((quality / total) * 100) if total > 0 else 0
                        signal_dbm = dbm_val
                    except Exception: pass
                else:
                    level_match = re.search(r'^\s*Signal level=(-?\d+)\s+dBm', current_cell_block, re.MULTILINE)
                    if level_match:
                        try:
                            signal_dbm = int(level_match.group(1))
                            if signal_dbm >= -50: signal_percent = 100
                            elif signal_dbm >= -60: signal_percent = 80
                            elif signal_dbm >= -67: signal_percent = 60
                            elif signal_dbm >= -70: signal_percent = 40
                            elif signal_dbm >= -80: signal_percent = 20
                            else: signal_percent = 10
                        except Exception: pass
                
                security_type = "Open"
                encryption_match = encryption_pattern.search(current_cell_block)
                if encryption_match and encryption_match.group(1) == 'on':
                    security_type = "WEP"
                    wpa_match = wpa_pattern.search(current_cell_block)
                    if wpa_match:
                        wpa_type = wpa_match.group(1)
                        if wpa_type == "RSN": security_type = "WPA2/WPA3"
                        else: security_type = wpa_type
                has_wps = bool(wps_pattern.search(current_cell_block))
                networks.append({
                    'ssid': ssid, 'signal': signal_percent, 'signal_dbm': signal_dbm,
                    'security': security_type, 'wps': has_wps, 'mac': cell_address
                })

    networks.sort(key=lambda x: x['signal_dbm'] if x['signal_dbm'] is not None else -100, reverse=True)
    
    logging.info(f"Scan parsing finished. Found {len(networks)} unique visible networks.")
    if not networks and output:
         logging.warning("iwlist scan produced output, but parsing resulted in zero networks. Check debug logs for raw output and regex patterns.")

    return networks

def get_saved_networks(interface: str) -> List[str]:
    conf_file = f"/etc/wpa_supplicant/wpa_supplicant-{interface}.conf"
    if not os.path.exists(conf_file):
        logging.warning(f"Config file not found: {conf_file}")
        return []
    try:
        content = _run_command(['cat', conf_file], use_sudo=True)
        if content is None:
            raise IOError("Failed to read config file even with sudo.")
        
        network_blocks = re.findall(r'network=\{(.*?)\}', content, re.DOTALL)
        ssids = set()
        for block in network_blocks:
            ssid_match = re.search(r'^\s*ssid="((?:[^"\\]|\\.)*)"', block, re.MULTILINE)
            if ssid_match:
                ssid = ssid_match.group(1).encode('utf-8').decode('unicode_escape', 'replace')
                ssids.add(ssid)
                
        return sorted(list(ssids))
        
    except Exception as e:
        logging.error(f"Could not read or parse config file {conf_file}: {e}")
        return []

def _is_editable(interface: str) -> bool:
    return interface == WLAN_EDITABLE

def connect(interface: str, ssid: str, password: Optional[str] = None) -> Dict[str, Any]:
    if not _is_editable(interface):
        return {"error": True, "message": f"Interface {interface} cannot be modified by this tool."}

    logging.info(f"Attempting to connect {interface} to SSID: {ssid}")
    
    if not _wait_for_socket(interface):
        return {"error": True, "message": "Wi-Fi service (wpa_supplicant) is not responding. Cannot connect."}
    
    current_status_raw = _run_command(_wpa_cli_command(interface, 'status'))
    if current_status_raw and f'ssid={ssid}' in current_status_raw and 'wpa_state=COMPLETED' in current_status_raw:
         logging.info(f"Already connected to {ssid}.")
         ip_status = get_interface_status(interface)
         return {"status": "success", "message": f"Already connected to {ssid}", "interface_status": ip_status}

    network_id = None
    
    list_networks_raw = _run_command(_wpa_cli_command(interface, 'list_networks'))
    if list_networks_raw:
        for line in list_networks_raw.splitlines()[1:]:
             parts = line.split('\t')
             try:
                 listed_ssid = parts[1].encode('utf-8').decode('unicode_escape', 'replace') if len(parts) >=2 else None
             except Exception:
                 listed_ssid = parts[1] if len(parts) >=2 else None

             if listed_ssid == ssid:
                 network_id = parts[0]
                 logging.info(f"Network profile for {ssid} already exists with ID {network_id}.")
                 break

    if network_id is None:
        add_network_raw = _run_command(_wpa_cli_command(interface, 'add_network'))
        if not add_network_raw or not add_network_raw.strip().isdigit():
            logging.error("Failed to add network profile via wpa_cli.")
            return {"error": True, "message": "Failed to create network profile."}
        network_id = add_network_raw.strip()
        logging.info(f"Added new network profile for {ssid} with ID {network_id}.")

        try:
             ssid_hex = ssid.encode('utf-8').hex()
             _run_command(_wpa_cli_command(interface, 'set_network', network_id, 'ssid', ssid_hex))
             logging.debug(f"Set SSID using hex: {ssid_hex}")
        except Exception:
             logging.warning("Could not encode SSID to hex, using quoted string.")
             _run_command(_wpa_cli_command(interface, 'set_network', network_id, 'ssid', f'"{ssid}"'))


        if password:
            psk_hash = _run_command(['wpa_passphrase', ssid, password])
            if psk_hash:
                 psk_match = re.search(r'^\s*psk=([0-9a-fA-F]{64})\s*$', psk_hash, re.MULTILINE)
                 if psk_match:
                     _run_command(_wpa_cli_command(interface, 'set_network', network_id, 'psk', psk_match.group(1)))
                     logging.debug("Set network PSK using hash from wpa_passphrase.")
                 else:
                      logging.warning("wpa_passphrase output did not contain expected PSK hash, using plaintext password.")
                      _run_command(_wpa_cli_command(interface, 'set_network', network_id, 'psk', f'"{password}"'))
            else:
                 logging.warning("wpa_passphrase command failed, using plaintext password.")
                 _run_command(_wpa_cli_command(interface, 'set_network', network_id, 'psk', f'"{password}"'))
        else:
            _run_command(_wpa_cli_command(interface, 'set_network', network_id, 'key_mgmt', 'NONE'))
    else:
        if password:
            logging.info(f"Re-applying password/PSK for existing network {ssid} (ID {network_id}).")
            psk_hash = _run_command(['wpa_passphrase', ssid, password])
            psk_match = psk_hash and re.search(r'^\s*psk=([0-9a-fA-F]{64})\s*$', psk_hash, re.MULTILINE)
            if psk_match:
                 _run_command(_wpa_cli_command(interface, 'set_network', network_id, 'psk', psk_match.group(1)))
            else:
                 _run_command(_wpa_cli_command(interface, 'set_network', network_id, 'psk', f'"{password}"'))
        elif _run_command(_wpa_cli_command(interface, 'get_network', network_id, 'key_mgmt')) != 'key_mgmt=NONE':
             logging.debug(f"Connecting to existing secured network {ssid} using potentially stored credentials.")


    _run_command(_wpa_cli_command(interface, 'enable_network', network_id))
    _run_command(_wpa_cli_command(interface, 'select_network', network_id))

    logging.info(f"Waiting up to 30s for connection completion and IP address for {ssid}...")
    connection_success = False
    ip_obtained = False
    start_time = time.time()
    last_wpa_state = None
    while time.time() - start_time < 30:
        time.sleep(1)
        cli_status_raw = _run_command(_wpa_cli_command(interface, 'status'))
        
        if not cli_status_raw:
            logging.error("wpa_cli status command failed during connection wait.")
            continue

        state_match = re.search(r'^wpa_state=(.*)$', cli_status_raw, re.MULTILINE)
        current_wpa_state = state_match.group(1) if state_match else "UNKNOWN"
        if current_wpa_state != last_wpa_state:
             logging.info(f"wpa_supplicant state: {current_wpa_state}")
             last_wpa_state = current_wpa_state


        ssid_match_in_status = re.search(r'^ssid=(.*)$', cli_status_raw, re.MULTILINE)
        current_ssid_in_status = ssid_match_in_status.group(1) if ssid_match_in_status else None
        
        if current_ssid_in_status == ssid and current_wpa_state == 'COMPLETED':
            if not connection_success:
                 logging.info("WPA handshake completed. Requesting IP address if needed...")
                 connection_success = True
                 _run_command(['dhcpcd', '-k', interface], use_sudo=True)
                 time.sleep(0.5)
                 _run_command(['dhcpcd', '-n', interface], use_sudo=True)

            ip_status = get_interface_status(interface)
            if ip_status.get('ip_address'):
                logging.info(f"IP address {ip_status['ip_address']} obtained.")
                ip_obtained = True
                break
            else:
                 logging.warning("Connected, but waiting for IP address...")
        
        elif current_wpa_state in ['DISCONNECTED', 'INACTIVE', 'INTERFACE_DISABLED']:
             reason_match = re.search(r'^reason=(.*)$', cli_status_raw, re.MULTILINE)
             fail_reason = reason_match.group(1) if reason_match else "Unknown reason"
             
             if fail_reason == 'WRONG_KEY':
                 logging.error("Connection failed: Incorrect password (WRONG_KEY).")
                 _run_command(_wpa_cli_command(interface, 'remove_network', network_id))
                 return {"error": True, "message": "Connection failed: Incorrect password."}
             elif fail_reason == 'SSID_NOT_FOUND':
                  logging.error(f"Connection failed: Network SSID '{ssid}' not found during scan.")
                  _run_command(_wpa_cli_command(interface, 'remove_network', network_id))
                  return {"error": True, "message": f"Connection failed: Network '{ssid}' not found."}

             logging.warning(f"wpa_supplicant state is {current_wpa_state}. Reason: {fail_reason}. Waiting...")
        
        elif current_wpa_state not in ['COMPLETED', 'UNKNOWN']:
             logging.info(f"wpa_supplicant state: {current_wpa_state}. Waiting...")


    if connection_success and ip_obtained:
        _run_command(_wpa_cli_command(interface, 'save_config'))
        final_ip_status = get_interface_status(interface)
        logging.info(f"Successfully connected to {ssid} with IP {final_ip_status.get('ip_address')}.")
        return {"status": "success", "message": f"Connected to {ssid}", "interface_status": final_ip_status}
    elif connection_success and not ip_obtained:
        logging.error("Connection established (WPA COMPLETED) but failed to obtain an IP address via DHCP.")
        return {"error": True, "message": "Connected but failed to get IP address from DHCP server. Check router/DHCP config."}
    else:
        logging.error(f"Connection timeout or failure for {ssid}. Final state: {last_wpa_state}")
        network_was_added_in_this_run = 'add_network_raw' in locals() and add_network_raw and add_network_raw.strip().isdigit() and add_network_raw.strip() == network_id
        if network_was_added_in_this_run:
             logging.info(f"Removing newly added network profile {network_id} due to connection failure.")
             _run_command(_wpa_cli_command(interface, 'remove_network', network_id))
        else:
             _run_command(_wpa_cli_command(interface, 'disable_network', network_id))
             _run_command(_wpa_cli_command(interface, 'save_config'))

        error_message = f"Failed to connect to '{ssid}'. Timeout or network not found."
        if last_wpa_state and last_wpa_state != 'COMPLETED':
             error_message = f"Failed to connect to '{ssid}'. Connection stopped at state: {last_wpa_state}."
             
        return {"error": True, "message": error_message}

def disconnect(interface: str) -> Dict[str, Any]:
    if not _is_editable(interface):
        return {"error": True, "message": f"Interface {interface} cannot be modified."}
        
    logging.info(f"Disconnecting interface {interface}...")
    
    if not _wait_for_socket(interface, timeout=5):
        logging.warning("Wi-Fi service not responding, but attempting disconnect anyway.")

    _run_command(_wpa_cli_command(interface, 'disconnect'))
    
    networks_raw = _run_command(_wpa_cli_command(interface, 'list_networks'))
    if networks_raw:
         for line in networks_raw.splitlines()[1:]:
             parts = line.split('\t')
             if len(parts) >= 4 and '[CURRENT]' in parts[3]:
                 logging.warning("wpa_cli disconnect command sent, but a network is still [CURRENT]. Disabling.")
                 _run_command(_wpa_cli_command(interface, 'disable_network', parts[0]))
                 break

    logging.info(f"Releasing DHCP lease for {interface}.")
    _run_command(['dhcpcd', '-k', interface], use_sudo=True)

    time.sleep(1)
    final_status = get_interface_status(interface)
    if final_status.get("state") == "disconnected" or final_status.get("ip_address") is None:
         logging.info(f"Interface {interface} appears disconnected.")
    else:
         logging.warning(f"Interface {interface} state after disconnect attempt: {final_status.get('state')}")
         
    return {"status": "success", "message": "Disconnect command sent."}

def forget_network(interface: str, ssid: str) -> Dict[str, Any]:
    if not _is_editable(interface):
        return {"error": True, "message": f"Interface {interface} cannot be modified."}
        
    logging.info(f"Forgetting network: {ssid} on interface {interface}...")

    if not _wait_for_socket(interface):
        logging.warning("Wi-Fi service not responding. Attempting to remove from config file only.")
        network_id_to_remove = None
    else:
        network_id_to_remove = None
        list_networks_raw = _run_command(_wpa_cli_command(interface, 'list_networks'))
        if list_networks_raw:
            for line in list_networks_raw.splitlines()[1:]:
                 parts = line.split('\t')
                 try:
                      listed_ssid = parts[1].encode('utf-8').decode('unicode_escape', 'replace') if len(parts) >=2 else None
                 except Exception:
                      listed_ssid = parts[1] if len(parts) >=2 else None

                 if listed_ssid == ssid:
                     network_id_to_remove = parts[0]
                     logging.debug(f"Found network ID {network_id_to_remove} for SSID {ssid} in current state.")
                     break
                     
        if network_id_to_remove:
            remove_result = _run_command(_wpa_cli_command(interface, 'remove_network', network_id_to_remove))
            if remove_result == "OK":
                logging.info(f"Removed network ID {network_id_to_remove} ({ssid}) from current session.")
            else:
                logging.warning(f"wpa_cli remove_network command failed or returned unexpected result: {remove_result}")
        else:
             logging.info(f"SSID {ssid} not found in current wpa_supplicant session networks.")

    conf_file = f"/etc/wpa_supplicant/wpa_supplicant-{interface}.conf"
    required_ctrl_line = f"ctrl_interface=DIR={WPA_SOCKET_PATH} GROUP=netdev"

    if not os.path.exists(conf_file):
        logging.warning(f"Config file {conf_file} not found. Nothing to forget persistently.")
        if network_id_to_remove:
             return {"status": "success", "message": f"Network {ssid} removed from session (config file not found)."}
        else:
             return {"status": "success", "message": f"Network {ssid} was not configured."}

    try:
        content = _run_command(['cat', conf_file], use_sudo=True)
        if content is None:
            raise IOError("Could not read config file with sudo.")
            
        escaped_ssid = re.escape(ssid)
        pattern = r'^\s*network\s*=\s*\{(?:[^{}]|\{(?:[^{}]|\{[^{}]*\})*\})*?\s*ssid="' + escaped_ssid + r'"(?:[^{}]|\{(?:[^{}]|\{[^{}]*\})*\})*?^\s*\}'
        
        networks_removed = 0
        def remove_match(match):
            nonlocal networks_removed
            networks_removed += 1
            logging.debug(f"Removing network block: {match.group(0)}")
            return ""

        new_content = re.sub(pattern, remove_match, content, flags=re.MULTILINE | re.DOTALL)

        if networks_removed > 0:
            logging.info(f"Removed {networks_removed} network block(s) for SSID {ssid} from config content.")
            final_content = new_content.strip()
            if not final_content:
                 final_content = f"{required_ctrl_line}\nupdate_config=1\n"
            else:
                if required_ctrl_line not in final_content:
                     final_content = f"{required_ctrl_line}\n{final_content}\n"
                if "update_config=1" not in final_content:
                     final_content = final_content.replace(required_ctrl_line, f"{required_ctrl_line}\nupdate_config=1", 1)


            escaped_content_write = final_content.replace("'", "'\\''")
            write_cmd = ['bash', '-c', f"echo '{escaped_content_write.strip()}\n' | sudo tee {conf_file} > /dev/null"]
            if _run_command(write_cmd, use_sudo=False) is None:
                 raise IOError("Failed to write updated config file using tee.")

            if _wait_for_socket(interface, timeout=2):
                 _run_command(_wpa_cli_command(interface, 'reconfigure'))
                 
            logging.info(f"Successfully forgot network {ssid} and updated config file.")
            return {"status": "success", "message": f"Forgot network {ssid}."}
        else:
            logging.warning(f"SSID '{ssid}' not found within any network block in config file {conf_file}. No changes made to file.")
            return {"status": "success", "message": f"Network {ssid} not found in configuration file."}
            
    except Exception as e:
        logging.error(f"Error forgetting network {ssid}: {e}", exc_info=True)
        return {"error": True, "message": "An error occurred while modifying the configuration file."}

def configure_ap(ssid: str, password: Optional[str] = None) -> Dict[str, Any]:
    if not ssid:
        return {"error": True, "message": "AP SSID cannot be empty."}
    if password and (len(password) < 8 or len(password) > 63):
        return {"error": True, "message": "Password must be 8-63 characters, or blank for open network."}

    conf_file_path = "/etc/hostapd/hostapd.conf"
    
    current_conf_content = _run_command(['cat', conf_file_path], use_sudo=True)
    if current_conf_content is None:
        logging.error(f"Failed to read current hostapd config at {conf_file_path}. Aborting.")
        return {"error": True, "message": "Failed to read hostapd configuration."}

    new_conf_lines = []
    lines = current_conf_content.splitlines()
    
    ssid_set = False
    psk_set = False
    wpa_passphrase_set = False
    wpa_set = False
    wpa_key_mgmt_set = False
    rsn_pairwise_set = False

    for line in lines:
        if line.strip().startswith('ssid='):
            new_conf_lines.append(f"ssid={ssid}")
            ssid_set = True
            continue
        if line.strip().startswith('wpa_passphrase='):
            wpa_passphrase_set = True
            if password:
                new_conf_lines.append(f"wpa_passphrase={password}")
                psk_set = True
            continue
        if line.strip().startswith('wpa='):
            wpa_set = True
            new_conf_lines.append(f"wpa={'2' if password else '0'}")
            continue
        if line.strip().startswith('wpa_key_mgmt='):
            wpa_key_mgmt_set = True
            if password:
                new_conf_lines.append("wpa_key_mgmt=WPA-PSK")
            continue
        if line.strip().startswith('rsn_pairwise='):
            rsn_pairwise_set = True
            if password:
                new_conf_lines.append("rsn_pairwise=CCMP")
            continue
        
        new_conf_lines.append(line)

    if not ssid_set: new_conf_lines.append(f"ssid={ssid}")
    
    if password:
        if not wpa_set: new_conf_lines.append("wpa=2")
        if not wpa_key_mgmt_set: new_conf_lines.append("wpa_key_mgmt=WPA-PSK")
        if not rsn_pairwise_set: new_conf_lines.append("rsn_pairwise=CCMP")
        if not wpa_passphrase_set: new_conf_lines.append(f"wpa_passphrase={password}")
    else:
        if not wpa_set: new_conf_lines.append("wpa=0")
        if not wpa_key_mgmt_set: new_conf_lines.append("wpa_key_mgmt=WPA-PSK")
        
    new_conf_content = "\n".join(new_conf_lines) + "\n"
    
    try:
        logging.info(f"Writing new configuration to {conf_file_path}...")
        escaped_content_write = new_conf_content.replace("'", "'\\''")
        write_cmd = ['bash', '-c', f"echo '{escaped_content_write.strip()}\n' | sudo tee {conf_file_path} > /dev/null"]
        if _run_command(write_cmd, use_sudo=False) is None:
             raise IOError("Failed to write updated hostapd config file using tee.")
             
        logging.info("Restarting hostapd service to apply new settings...")
        _run_command(['systemctl', 'restart', 'hostapd.service'], use_sudo=True)
        time.sleep(2)

        status_output = _run_command(['systemctl', 'is-active', 'hostapd.service'], use_sudo=True)
        if status_output == 'active':
            logging.info("hostapd.service restarted successfully.")
            return {"status": "success", "message": "AP settings updated and service restarted."}
        else:
            logging.error("CRITICAL: FAILED TO RESTART hostapd.service after config change.")
            status_log = _run_command(['systemctl', 'status', 'hostapd.service', '--no-pager', '-l'], use_sudo=True)
            journal_log = _run_command(['journalctl', '-u', 'hostapd.service', '-n', '20', '--no-pager'], use_sudo=True)
            logging.error(f"--- Service Status Log ---\n{status_log}\n--- End of Status Log ---")
            logging.error(f"--- Service Journal Log ---\n{journal_log}\n--- End of Journal Log ---")
            return {"error": True, "message": "Failed to restart AP service. Check logs."}

    except Exception as e:
        logging.error(f"Error configuring AP: {e}", exc_info=True)
        return {"error": True, "message": "An error occurred while saving AP configuration."}


def initialize_wifi_mode():
    logging.info("Initializing Wi-Fi mode for wlan1...")
    try:
        hostapd_conf = "/etc/hostapd/hostapd.conf"
        if not os.path.exists(hostapd_conf):
             logging.warning(f"{hostapd_conf} not found. Cannot automatically switch to AP mode. Please configure hostapd.")
             current_status = get_interface_status(WLAN_EDITABLE)
             logging.warning(f"wlan1 will remain in its current mode ({current_status.get('mode', 'unknown')}).")
             return

        current_status = get_interface_status(WLAN_EDITABLE)
        current_mode = current_status.get("mode")
        
        target_mode = "ap" 
        
        if current_mode == target_mode:
            logging.info(f"wlan1 is already in the desired startup mode ({target_mode}). No changes needed.")
        else:
            logging.info(f"wlan1 is in '{current_mode}' mode. Switching to desired startup mode ({target_mode})...")
            set_mode_wlan1(target_mode)
            
        final_status = get_interface_status(WLAN_EDITABLE)
        logging.info(f"Final wlan1 mode after initialization: {final_status.get('mode')}")
        
    except Exception as e:
        logging.error(f"Error during automatic Wi-Fi mode initialization: {e}", exc_info=True)

if __name__ != "__main__":
    if "pytest" not in sys.modules:
        initialize_wifi_mode()
elif __name__ == "__main__":
     print("Running wifi_manager.py directly (for testing maybe?)")
     print("Current Status:", get_status())
