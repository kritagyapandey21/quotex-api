"""
WebSocket transport using undetected-chromedriver to bypass Cloudflare.
Intercepts WebSocket traffic using Chrome DevTools Protocol (CDP).
"""

import json
import queue
import threading
import time
from typing import Optional, Dict, Callable
import undetected_chromedriver as uc
from selenium.common.exceptions import WebDriverException
from ..utils.logger import get_logger

logger = get_logger(__name__)


class ChromeWebSocketTransport:
    """WebSocket transport using Chrome browser to bypass Cloudflare."""
    
    def __init__(self, url: str, headers: Optional[Dict[str, str]] = None):
        """
        Initialize Chrome WebSocket transport.
        
        Args:
            url: WebSocket URL
            headers: Optional HTTP headers (not used directly, Chrome handles this)
        """
        self.url = url
        self.headers = headers or {}
        self.driver = None
        self.message_queue = queue.Queue()
        self.running = False
        self._on_message: Optional[Callable] = None
        self._on_error: Optional[Callable] = None
        self._on_close: Optional[Callable] = None
        self._ws_connected = False
        self._monitor_thread: Optional[threading.Thread] = None
        
    def connect(self) -> bool:
        """
        Connect via Chrome browser.
        
        Returns:
            True if connection successful, False otherwise
        """
        print("[CHROME] Starting undetected Chrome...")
        try:
            # Create Chrome options
            options = uc.ChromeOptions()
            
            # Try headless first (may not work with Cloudflare)
            options.add_argument('--headless=new')
            options.add_argument('--disable-gpu')
            options.add_argument('--no-sandbox')
            options.add_argument('--disable-dev-shm-usage')
            
            # Additional stealth options
            options.add_argument('--disable-blink-features=AutomationControlled')
            
            print("[CHROME] Launching browser...")
            self.driver = uc.Chrome(options=options, version_main=None)
            
            # Enable Network domain in CDP to intercept WebSocket
            print("[CHROME] Enabling CDP Network domain...")
            self.driver.execute_cdp_cmd('Network.enable', {})
            
            # Navigate to Quotex to establish session
            print("[CHROME] Loading Quotex...")
            self.driver.get("https://quotex.com")
            
            # Wait a bit for page to load
            time.sleep(2)
            
            # Check if Cloudflare blocked us
            page_source = self.driver.page_source.lower()
            if "challenge" in page_source or "just a moment" in page_source:
                print("[CHROME] ⚠️ Cloudflare challenge detected - trying to wait...")
                time.sleep(5)  # Wait for challenge to complete
                page_source = self.driver.page_source.lower()
                if "challenge" in page_source or "just a moment" in page_source:
                    logger.error("Cloudflare challenge not completed")
                    return False
            
            print("[CHROME] ✅ Page loaded successfully")
            
            # Extract WebSocket URL from the page
            ws_url = self._extract_websocket_url()
            if not ws_url:
                logger.error("Failed to extract WebSocket URL from page")
                return False
            
            print(f"[CHROME] WebSocket URL: {ws_url}")
            
            # Set up WebSocket monitoring
            self.running = True
            self._monitor_thread = threading.Thread(
                target=self._monitor_websocket,
                daemon=True,
                name="ChromeWSMonitor"
            )
            self._monitor_thread.start()
            
            # Give it time to connect
            timeout = 10
            start_time = time.time()
            while not self._ws_connected and time.time() - start_time < timeout:
                time.sleep(0.1)
            
            if not self._ws_connected:
                logger.warning("WebSocket not connected after timeout")
            
            logger.info("Chrome WebSocket transport established")
            return True
            
        except Exception as e:
            logger.error(f"Failed to start Chrome: {e}")
            print(f"[CHROME] Error: {e}")
            if self._on_error:
                self._on_error(e)
            return False
    
    def _extract_websocket_url(self) -> Optional[str]:
        """Extract WebSocket URL from page."""
        try:
            # Try to find WebSocket URL in network logs
            logs = self.driver.get_log('performance')
            for entry in logs:
                message = json.loads(entry['message'])
                method = message.get('message', {}).get('method')
                if method == 'Network.webSocketCreated':
                    params = message.get('message', {}).get('params', {})
                    url = params.get('url')
                    if url and 'socket.io' in url:
                        return url
            
            # Fallback: construct URL manually
            return self.url
            
        except Exception as e:
            logger.error(f"Failed to extract WebSocket URL: {e}")
            return None
    
    def _monitor_websocket(self):
        """Monitor WebSocket traffic via CDP."""
        print("[CHROME] Starting WebSocket monitor...")
        
        while self.running:
            try:
                # Get performance logs which include WebSocket frames
                logs = self.driver.get_log('performance')
                
                for entry in logs:
                    try:
                        message = json.loads(entry['message'])
                        method = message.get('message', {}).get('method')
                        params = message.get('message', {}).get('params', {})
                        
                        if method == 'Network.webSocketCreated':
                            print(f"[CHROME] WebSocket created: {params.get('url')}")
                            self._ws_connected = True
                            
                        elif method == 'Network.webSocketFrameReceived':
                            # Received message from server
                            response = params.get('response', {})
                            payload = response.get('payloadData')
                            if payload:
                                print(f"[CHROME] << {payload[:200]}")
                                self.message_queue.put(payload)
                                if self._on_message:
                                    self._on_message(payload)
                                    
                        elif method == 'Network.webSocketFrameSent':
                            # Sent message to server
                            request = params.get('request', {})
                            payload = request.get('payloadData')
                            if payload:
                                print(f"[CHROME] >> {payload[:100]}")
                                
                        elif method == 'Network.webSocketClosed':
                            print("[CHROME] WebSocket closed")
                            self._ws_connected = False
                            if self._on_close:
                                self._on_close()
                                
                    except Exception as e:
                        # Skip malformed log entries
                        pass
                
                time.sleep(0.1)  # Small delay to avoid hammering
                
            except WebDriverException:
                # Browser closed
                print("[CHROME] Browser closed")
                break
            except Exception as e:
                if self.running:
                    logger.error(f"Monitor error: {e}")
                time.sleep(1)
        
        print("[CHROME] WebSocket monitor stopped")
    
    def send(self, message: str) -> bool:
        """
        Send message via WebSocket using Chrome.
        
        Args:
            message: Message to send
            
        Returns:
            True if sent successfully
        """
        if not self.driver or not self._ws_connected:
            print("[CHROME] ERROR: Not connected")
            return False
        
        try:
            # Execute JavaScript to send WebSocket message
            # We need to inject code that accesses the WebSocket object
            script = f"""
            // Find WebSocket connection
            if (window._quotex_ws) {{
                window._quotex_ws.send({json.dumps(message)});
                return true;
            }}
            return false;
            """
            
            result = self.driver.execute_script(script)
            
            if not result:
                # Try alternative: send via CDP
                print("[CHROME] Trying to send via CDP...")
                # Note: Sending via CDP is complex, may need to find request ID
                logger.warning("WebSocket send via CDP not fully implemented")
            
            return result or False
            
        except Exception as e:
            logger.error(f"Failed to send: {e}")
            print(f"[CHROME] Send error: {e}")
            return False
    
    def receive(self, timeout: Optional[float] = None) -> Optional[str]:
        """
        Receive message from queue.
        
        Args:
            timeout: Optional timeout in seconds
            
        Returns:
            Message string or None if timeout
        """
        try:
            return self.message_queue.get(timeout=timeout)
        except queue.Empty:
            return None
    
    def close(self):
        """Close the connection."""
        print("[CHROME] Closing connection...")
        self.running = False
        
        if self._monitor_thread and self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=2)
        
        if self.driver:
            try:
                self.driver.quit()
            except:
                pass
        
        logger.info("Chrome WebSocket connection closed")
    
    def set_on_message(self, callback: Callable):
        """Set message callback."""
        self._on_message = callback
    
    def set_on_error(self, callback: Callable):
        """Set error callback."""
        self._on_error = callback
    
    def set_on_close(self, callback: Callable):
        """Set close callback."""
        self._on_close = callback
