import serial
import time
import json
import logging
import argparse
import requests
from datetime import datetime

# 🧾 Structured logging
logging.basicConfig(
    filename='rfid_reader.log',
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)

class FrameBuilder:
    @staticmethod
    def build(cmd_type, cmd_code, payload):
        length = len(payload)
        frame = bytearray([0xBB, cmd_type, cmd_code, length >> 8, length & 0xFF])
        frame.extend(payload)
        checksum = sum(frame[1:]) & 0xFF
        frame.extend([checksum, 0x7E])
        return bytes(frame)

class RFIDReader:
    def __init__(self, config_path='config.json'):
        with open(config_path) as f:
            cfg = json.load(f)
        self.port = cfg.get('serial_port', '/dev/ttyUSB0')
        self.baudrate = cfg.get('baudrate', 115200)
        self.rf_power = cfg.get('rf_power', 20.0)
        self.region = cfg.get('region', 'China2')
        self.channel = cfg.get('channel', 0)
        
        # API configuration
        self.api_base_url = cfg.get('api_base_url', 'https://36s3748yo1.execute-api.us-east-2.amazonaws.com/prod')
        self.rfid_endpoint = cfg.get('rfid_endpoint', '/rfid')
        self.device_id = cfg.get('device_id', 'raspberry_pi_001')
        
        self.ser = serial.Serial(self.port, self.baudrate, timeout=1)
        self.epc_seen = set()
        self.tag_log_path = '/Users/thiwankajayasiri/iot-pj-aut/rpi-pkg/rfid_tags.log'
        
    def log_tag(self, tag: dict):
        with open(self.tag_log_path, 'a') as f:
            f.write(json.dumps(tag) + '\n')
    
    def send_to_api(self, tag: dict):
        """Send RFID tag data to API endpoint"""
        try:
            data = {
                "rfid_tag": tag['EPC'],
                "timestamp": tag['timestamp'],
                "device_id": self.device_id,
                "user_info": {
                    "rssi": tag['RSSI'],
                    "pc": tag['PC'],
                    "crc": tag['CRC']
                }
            }
            
            response = requests.post(
                f"{self.api_base_url}{self.rfid_endpoint}",
                json=data,
                headers={'Content-Type': 'application/json'},
                timeout=30
            )
            
            if response.status_code == 200:
                logging.info(f"✅ RFID data sent to API successfully: {tag['EPC']}")
                print(f"✅ API: RFID data sent - {tag['EPC']}")
            else:
                logging.error(f"❌ Failed to send RFID data to API: {response.status_code} - {response.text}")
                print(f"❌ API Error: {response.status_code}")
                
        except Exception as e:
            logging.error(f"❌ Error sending RFID data to API: {e}")
            print(f"❌ API Error: {e}")
        
    def send_command(self, cmd: bytes, retries=3):
        for attempt in range(retries):
            try:
                self.ser.write(cmd)
                time.sleep(0.1)
                response = self.ser.read(128)
                if response:
                    self.log_raw(response)
                    return response
                logging.warning(f"Retry {attempt + 1} failed.")
                time.sleep(0.5)
            except serial.SerialException as e:
                logging.error(f"Serial error in send_command: {e}")
                if attempt < retries - 1:
                    self.reconnect_serial()
                    time.sleep(1)
                else:
                    raise
        logging.error("All retries failed.")
        return None

    def log_raw(self, frame: bytes):
        hex_str = frame.hex().upper()
        timestamp = datetime.now().isoformat()
        with open('/Users/thiwankajayasiri/iot-pj-aut/rpi-pkg/rfid_raw.log', 'a') as f:
            f.write(f"{timestamp} {hex_str}\n")

    def parse_tag_frame(self, frame: bytes):
        if frame[:3] != b'\xBB\x02\x22':
            return None
        rssi = frame[6]
        pc = frame[7:9]
        epc = frame[9:21]
        crc = frame[21:23]
        return {
            'timestamp': datetime.now().isoformat(),
            'EPC': epc.hex().upper(),
            'RSSI': -(256 - rssi),
            'PC': pc.hex().upper(),
            'CRC': crc.hex().upper()
        }

    def parse_error_frame(self, frame: bytes):
        if frame and frame[:3] == b'\xBB\x01\xFF':
            error_code = frame[5]
            logging.error(f"Reader Error Code: {error_code:02X}")
            print(f"Reader Error: Code {error_code:02X}")

    def single_inventory(self):
        cmd = FrameBuilder.build(0x00, 0x22, [])
        response = self.send_command(cmd)
        if response:
            tag = self.parse_tag_frame(response)
            if tag:
                logging.info(f"Tag Read: {tag}")
                print(tag)
                # Send to API
                self.send_to_api(tag)
            else:
                print("No tag detected.")
        else:
            print("No response from reader.")

    def multi_inventory(self, count=1000):
        payload = [0x22] + list(count.to_bytes(2, 'big'))
        cmd = FrameBuilder.build(0x00, 0x27, payload)
        self.send_command(cmd)

    def stop_multi_inventory(self):
        cmd = FrameBuilder.build(0x00, 0x28, [])
        self.send_command(cmd)

    def read_multiple_tags(self, duration=5, throttle=0.1, max_retries=3):
        self.multi_inventory()
        start = time.time()
        retries = 0
        print("Reading tags...")

        while time.time() - start < duration:
            frame = self.ser.read(64)
            if frame.startswith(b'\xBB\x02\x22'):
                tag = self.parse_tag_frame(frame)
                if tag and tag['EPC'] not in self.epc_seen:
                    self.epc_seen.add(tag['EPC'])
                    logging.info(f"Tag: {tag}")
                    self.log_tag(tag)
                    print(tag)
                    # Send to API
                    self.send_to_api(tag)
                retries = 0  # reset on success
            elif frame.startswith(b'\xBB\x01\xFF'):
                self.parse_error_frame(frame)
                retries += 1
                if retries >= max_retries:
                    logging.warning("Max retries reached. Stopping inventory.")
                    break
            time.sleep(throttle)
        self.stop_multi_inventory()

    def stream_tags(self):
        """Continuous RFID tag streaming"""
        try:
            self.multi_inventory()
            print("🔄 RFID streaming started... Press Ctrl+C to stop")
            
            while True:
                try:
                    frame = self.ser.read(64)
                    if frame.startswith(b'\xBB\x02\x22'):
                        tag = self.parse_tag_frame(frame)
                        if tag and tag['EPC'] not in self.epc_seen:
                            self.epc_seen.add(tag['EPC'])
                            logging.info(f"Tag: {tag}")
                            self.log_tag(tag)
                            print(tag)
                            self.send_to_api(tag)
                    elif frame.startswith(b'\xBB\x01\xFF'):
                        self.parse_error_frame(frame)
                    time.sleep(0.1)
                except serial.SerialException as e:
                    logging.error(f"Serial error: {e}")
                    print(f"❌ Serial error: {e}")
                    print("🔄 Attempting to reconnect...")
                    self.reconnect_serial()
                    time.sleep(2)
                except Exception as e:
                    logging.error(f"Unexpected error: {e}")
                    print(f"❌ Error: {e}")
                    time.sleep(1)
        except KeyboardInterrupt:
            print("\n🛑 Streaming stopped by user")
        finally:
            try:
                self.stop_multi_inventory()
            except:
                pass  # Ignore errors during cleanup

    def set_rf_power(self):
        power_map = {
            18.5: [0x04, 0xE2],
            20.0: [0x05, 0x78],
            21.5: [0x06, 0x0E],
            23.0: [0x06, 0xA4],
            24.5: [0x07, 0x3A],
            26.0: [0x07, 0xD0]
        }
        payload = power_map.get(self.rf_power)
        if not payload:
            raise ValueError("Unsupported RF power level")
        cmd = FrameBuilder.build(0x00, 0xB6, payload)
        self.send_command(cmd)

    def set_region(self):
        region_map = {
            'China2': [0x01, 0x09],
            'China1': [0x04, 0x0C],
            'US':     [0x02, 0x0A],
            'Europe': [0x03, 0x0B],
            'Korea':  [0x06, 0x0E]
        }
        payload = region_map.get(self.region)
        if not payload:
            raise ValueError("Unsupported region")
        cmd = FrameBuilder.build(0x00, 0x07, payload)
        self.send_command(cmd)

    def set_channel(self):
        if not (0 <= self.channel <= 0x33):
            raise ValueError("Channel out of range")
        cmd = FrameBuilder.build(0x00, 0xAB, [self.channel])
        self.send_command(cmd)

    def set_select_epc(self, epc_hex: str):
        epc_bytes = bytes.fromhex(epc_hex)
        payload = [0x00, 0x00, 0x20, 0x00, len(epc_bytes)] + list(epc_bytes)
        cmd = FrameBuilder.build(0x00, 0x0C, payload)
        self.send_command(cmd)

    def read_tag_memory(self, bank: int, offset: int, count: int, password: bytes = b'\x00\x00\x00\x00'):
        payload = [bank] + list(password) + list(offset.to_bytes(2, 'big')) + list(count.to_bytes(2, 'big'))
        cmd = FrameBuilder.build(0x00, 0x39, payload)
        response = self.send_command(cmd)
        if response and response[2] == 0x39:
            data = response[7:-3]
            hex_data = data.hex().upper()
            logging.info(f"Memory Read: Bank={bank}, Offset={offset}, Count={count}, Data={hex_data}")
            print(f"Memory Read: {hex_data}")
        else:
            self.parse_error_frame(response)

    def write_tag_memory(self, bank: int, offset: int, data: bytes, password: bytes = b'\x00\x00\x00\x00'):
        count = len(data) // 2
        payload = [bank] + list(password) + list(offset.to_bytes(2, 'big')) + list(count.to_bytes(2, 'big')) + list(data)
        cmd = FrameBuilder.build(0x00, 0x49, payload)
        response = self.send_command(cmd)
        if response and response[2] == 0x49:
            logging.info(f"Memory Write Success: Bank={bank}, Offset={offset}")
            print("Memory Write Successful")
        else:
            self.parse_error_frame(response)

    def reconnect_serial(self):
        """Reconnect to serial port"""
        try:
            if self.ser.is_open:
                self.ser.close()
            time.sleep(1)
            self.ser = serial.Serial(self.port, self.baudrate, timeout=1)
            logging.info(f"Reconnected to {self.port}")
            print(f"✅ Reconnected to {self.port}")
        except Exception as e:
            logging.error(f"Failed to reconnect: {e}")
            print(f"❌ Failed to reconnect: {e}")

    def close(self):
        if self.ser.is_open:
            self.ser.close()

# 🚀 CLI Interface
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RFID Reader CLI")
    parser.add_argument('--setup', action='store_true', help="Set region, channel, and RF power")
    parser.add_argument('--single', action='store_true', help="Perform single inventory")
    parser.add_argument('--multi', type=int, help="Perform multi inventory for N seconds")
    parser.add_argument('--stream', action='store_true', help="Continuous streaming mode")
    parser.add_argument('--select', type=str, help="Set EPC filter (hex)")
    parser.add_argument('--readmem', nargs=3, metavar=('BANK', 'OFFSET', 'COUNT'), help="Read tag memory")
    parser.add_argument('--writemem', nargs=3, metavar=('BANK', 'OFFSET', 'DATA'), help="Write tag memory (hex)")
    parser.add_argument('--duration', type=int, default=5, help="Duration for multi-inventory")
    parser.add_argument('--throttle', type=float, default=0.1, help="Throttle between reads")
    args = parser.parse_args()

    reader = RFIDReader()

    if args.setup:
        reader.set_region()
        reader.set_channel()
        reader.set_rf_power()

    if args.select:
        reader.set_select_epc(args.select)

    if args.single:
        reader.single_inventory()

    if args.multi:
        reader.read_multiple_tags(duration=args.duration, throttle=args.throttle)

    if args.stream:
        reader.stream_tags()

    if args.readmem:
        bank = int(args.readmem[0])
        offset = int(args.readmem[1])
        count