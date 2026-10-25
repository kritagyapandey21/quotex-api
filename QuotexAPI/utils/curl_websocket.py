"""
HTTP polling transport using curl_cffi to bypass Cloudflare.
Uses Socket.IO HTTP long-polling for both sending and receiving.
"""

import threading
import queue
import time
import re
import json as json_module
from typing import Optional, Dict, Callable
from curl_cffi import requests
from curl_cffi.const import CurlWsFlag
from ..utils.logger import get_logger

logger = get_logger(__name__)


class CurlWebSocketTransport:
    """HTTP polling transport using curl_cffi (bypasses Cloudflare)."""
    
    def __init__(self, url: str, headers: Optional[Dict[str, str]] = None):
        """
        Initialize HTTP polling transport.
        
        Args:
            url: WebSocket URL (will be converted to HTTP polling)
            headers: Optional HTTP headers
        """
        self.url = url
        self.headers = headers or {}
        self.session = None
        self.sid = None
        self.polling_url = None
        self.message_queue = queue.Queue()
        self.running = False
        self.poller_thread: Optional[threading.Thread] = None
        self._ws_conn = None
        self._on_message: Optional[Callable] = None
        self._on_error: Optional[Callable] = None
        self._on_close: Optional[Callable] = None
        
    def connect(self) -> bool:
        """
        Connect via HTTP polling with retry logic.
        
        Returns:
            True if connection successful, False otherwise
        """
        if self._connect_websocket():
            return True

        print("[HTTP POLLING] Connecting...")

        # Retry logic to deal with Cloudflare's intermittent blocking
        max_retries = 3
        retry_delay = 2
        
        for attempt in range(max_retries):
            try:
                if attempt > 0:
                    print(f"[HTTP POLLING] Retry attempt {attempt + 1}/{max_retries}...")
                    time.sleep(retry_delay * attempt)  # Exponential backoff
                
                # Convert WebSocket URL to HTTP polling URL
                base_url = self.url.replace('wss://', 'https://').replace('ws://', 'http://')
                base_url = base_url.replace('transport=websocket', 'transport=polling')
                self.polling_url = base_url
                
                # Create session with browser impersonation
                self.session = requests.Session(impersonate="chrome110")
                
                headers = {
                    'Origin': 'https://quotex.com',
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                    'Accept': '*/*',
                    'Accept-Language': 'en-US,en;q=0.9',
                    **self.headers
                }
                
                # Initial handshake
                print(f"[HTTP POLLING] GET {base_url}")
                response = self.session.get(base_url, headers=headers, timeout=10)
                
                if response.status_code == 403:
                    print(f"[HTTP POLLING] Cloudflare blocked (403), attempt {attempt + 1}/{max_retries}")
                    if attempt < max_retries - 1:
                        continue
                    else:
                        logger.error("Handshake blocked by Cloudflare after all retries")
                        return False
                
                if response.status_code != 200:
                    logger.error(f"Handshake failed: {response.status_code}")
                    if attempt < max_retries - 1:
                        continue
                    return False
                
                print(f"[HTTP POLLING] Handshake response: {response.text[:200]}")
                
                # Parse polling response format: <length>:<message><length>:<message>...
                # Example: 96:0{"sid":"..."}2:40
                messages = self._parse_polling_payload(response.text)
                
                # Extract SID from handshake
                handshake_msg = None
                for msg in messages:
                    if msg.startswith('0{'):
                        handshake = json_module.loads(msg[1:])
                        self.sid = handshake.get('sid')
                        handshake_msg = msg
                        print(f"[HTTP POLLING] Got SID: {self.sid}")
                        break
                
                if not self.sid:
                    logger.error("Failed to extract SID from handshake")
                    if attempt < max_retries - 1:
                        continue
                    return False
                
                # Process initial messages (like namespace connect "2:40")
                for msg in messages:
                    if msg != handshake_msg and self._on_message:
                        try:
                            self._on_message(msg)
                        except Exception as e:
                            logger.error(f"Callback error: {e}")
                
                self.running = True
                
                # Start polling thread
                self.poller_thread = threading.Thread(
                    target=self._poll_messages,
                    daemon=True,
                    name="HTTPPoller"
                )
                self.poller_thread.start()
                
                logger.info("HTTP polling connection established")
                return True
                
            except Exception as e:
                logger.error(f"Connection attempt {attempt + 1} failed: {e}")
                print(f"[HTTP POLLING] Connection error: {e}")
                if attempt < max_retries - 1:
                    continue
                if self._on_error:
                    self._on_error(e)
                return False
        
        # All retries failed
        logger.error("Failed to connect after all retries")
        return False
    
    def _connect_websocket(self) -> bool:
        """Try a native WebSocket upgrade (needs a valid Cloudflare clearance cookie)."""
        try:
            headers = {
                'Origin': 'https://quotex.com',
                'Accept-Language': 'en-US,en;q=0.9',
                **self.headers,
            }
            self.session = requests.Session(impersonate="chrome124")
            print(f"[WEBSOCKET] Connecting to {self.url}")
            self._ws_conn = self.session.ws_connect(self.url, headers=headers, timeout=15)
        except Exception as e:
            print(f"[WEBSOCKET] Upgrade failed ({str(e)[:120]}), falling back to polling")
            self._ws_conn = None
            return False

        self.running = True
        self.sid = "websocket"
        self.poller_thread = threading.Thread(
            target=self._ws_read_loop, daemon=True, name="WSReader"
        )
        self.poller_thread.start()
        logger.info("WebSocket connection established")
        return True

    def _ws_read_loop(self):
        """Read frames from the native WebSocket in a background thread."""
        while self.running and self._ws_conn is not None:
            try:
                data, _flags = self._ws_conn.recv()
            except Exception as e:
                if self.running:
                    logger.error(f"WebSocket read error: {e}")
                break
            msg = data.decode('utf-8', errors='replace') if isinstance(data, bytes) else data
            self.message_queue.put(msg)
            if self._on_message:
                try:
                    self._on_message(msg)
                except Exception as e:
                    logger.error(f"Callback error: {e}")
        self.running = False
        if self._on_close:
            self._on_close()

    def _parse_polling_payload(self, payload: str) -> list:
        """Parse Socket.IO polling payload format: <length>:<message>..."""
        messages = []
        while payload:
            # Find the colon separator
            colon_idx = payload.find(':')
            if colon_idx == -1:
                break
            
            try:
                # Extract length
                length = int(payload[:colon_idx])
                # Extract message
                message = payload[colon_idx + 1:colon_idx + 1 + length]
                messages.append(message)
                # Move to next message
                payload = payload[colon_idx + 1 + length:]
            except (ValueError, IndexError):
                break
        
        return messages
    
    def _poll_messages(self):
        """Poll for messages in background thread."""
        print("[HTTP POLLING] Starting poller thread")
        
        headers = {
            'Origin': 'https://quotex.com',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': '*/*',
            **self.headers
        }
        
        while self.running and self.sid:
            try:
                url = f"{self.polling_url}&sid={self.sid}"
                
                # Poll for messages (long-polling GET)
                response = self.session.get(url, headers=headers, timeout=25)
                
                if response.status_code == 200 and response.text:
                    print(f"[HTTP POLLING] Received: {response.text[:300]}")
                    
                    # Parse messages
                    messages = self._parse_polling_payload(response.text)
                    
                    for msg in messages:
                        # Put in queue
                        self.message_queue.put(msg)
                        
                        # Call callback
                        if self._on_message:
                            try:
                                self._on_message(msg)
                            except Exception as e:
                                logger.error(f"Callback error: {e}")
                
                elif response.status_code != 200:
                    logger.warning(f"Poll failed: {response.status_code}")
                    print(f"Poll failed: {response.status_code}")
                    time.sleep(1)
                    
            except Exception as e:
                if self.running:
                    logger.error(f"Poll error: {e}")
                    print(f"[HTTP POLLING] Poll error: {e}")
                time.sleep(1)
        
        print("[HTTP POLLING] Poller thread exiting")
        
        if self._on_close:
            self._on_close()
    
    def send(self, message: str) -> bool:
        """
        Send message via HTTP POST.
        
        Args:
            message: Socket.IO message (e.g., "42[...]")
            
        Returns:
            True if sent successfully
        """
        if not self.session or not self.sid:
            print("[HTTP POLLING] ERROR: Not connected")
            return False

        if self._ws_conn is not None:
            try:
                self._ws_conn.send(message, CurlWsFlag.TEXT)
                return True
            except Exception as e:
                logger.error(f"WebSocket send error: {e}")
                return False

        try:
            url = f"{self.polling_url}&sid={self.sid}"
            headers = {
                'Origin': 'https://quotex.com',
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Content-Type': 'text/plain;charset=UTF-8',
                'Accept': '*/*',
                **self.headers
            }
            
            print(f"[HTTP POLLING] POST: {message[:150]}")
            response = self.session.post(url, headers=headers, data=message, timeout=10)
            
            print(f"[HTTP POLLING] Send response ({response.status_code}): {response.text[:200]}")
            
            return response.status_code == 200
            
        except Exception as e:
            logger.error(f"Send error: {e}")
            print(f"[HTTP POLLING] Send error: {e}")
            if self._on_error:
                self._on_error(e)
            return False
    
    def recv(self, timeout: Optional[float] = None) -> Optional[str]:
        """Receive message from queue."""
        try:
            return self.message_queue.get(timeout=timeout)
        except queue.Empty:
            return None
    
    def set_on_message(self, callback: Callable[[str], None]):
        """Set callback for incoming messages."""
        self._on_message = callback
    
    def set_on_error(self, callback: Callable[[Exception], None]):
        """Set callback for errors."""
        self._on_error = callback
    
    def set_on_close(self, callback: Callable[[], None]):
        """Set callback for connection close."""
        self._on_close = callback
    
    def close(self):
        """Close the connection."""
        self.running = False
        self.sid = None
        if self._ws_conn is not None:
            try:
                self._ws_conn.close()
            except Exception:
                pass
            self._ws_conn = None
        logger.info("Transport connection closed")
    
    def is_connected(self) -> bool:
        """Check if connection is active."""
        return self.running and self.sid is not None
