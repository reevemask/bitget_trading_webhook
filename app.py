from flask import Flask, request, jsonify
import requests
import os
import json
import hmac
import hashlib
import time
import base64  # base64 import 추가!
from datetime import datetime
import logging
import threading
from typing import Dict, Optional, Tuple
import pickle

app = Flask(__name__)

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('trading.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# 환경 변수 설정
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', 'YOUR_BOT_TOKEN_HERE')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', 'YOUR_CHAT_ID_HERE')

# Bitget API 설정
BITGET_API_KEY = os.environ.get('BITGET_API_KEY', 'YOUR_API_KEY_HERE')
BITGET_SECRET_KEY = os.environ.get('BITGET_SECRET_KEY', 'YOUR_SECRET_KEY_HERE')
BITGET_PASSPHRASE = os.environ.get('BITGET_PASSPHRASE', 'YOUR_PASSPHRASE_HERE')
BITGET_BASE_URL = "https://api.bitget.com"

# 거래 설정
LOSS_RATIO = float(os.environ.get('LOSS_RATIO', '15'))  # 손실 비율 (%)
MAX_LEVERAGE = 30  # 최대 레버리지
STATS_FILE = 'trading_stats.pkl'  # 통계 파일

# 현재 활성 포지션 (메모리에 저장)
current_position = None
position_lock = threading.Lock()

# 거래 통계
class TradingStats:
    def __init__(self):
        self.wins = 0
        self.losses = 0
        self.total_trades = 0
        self.start_date = datetime.now()
        self.trades_history = []
    
    def add_trade(self, result: str, profit_rate: float, symbol: str):
        self.total_trades += 1
        if result == 'WIN':
            self.wins += 1
        else:
            self.losses += 1
        
        self.trades_history.append({
            'timestamp': datetime.now(),
            'symbol': symbol,
            'result': result,
            'profit_rate': profit_rate
        })
    
    def get_win_rate(self):
        if self.total_trades == 0:
            return 0
        return (self.wins / self.total_trades) * 100
    
    def reset(self):
        self.wins = 0
        self.losses = 0
        self.total_trades = 0
        self.start_date = datetime.now()
        self.trades_history = []
    
    def save(self):
        try:
            with open(STATS_FILE, 'wb') as f:
                pickle.dump(self, f)
        except Exception as e:
            logger.error(f"통계 저장 실패: {str(e)}")
    
    @classmethod
    def load(cls):
        try:
            if os.path.exists(STATS_FILE):
                with open(STATS_FILE, 'rb') as f:
                    return pickle.load(f)
        except Exception as e:
            logger.error(f"통계 로드 실패: {str(e)}")
        return cls()

# 통계 객체 초기화
stats = TradingStats.load()

class BitgetFuturesClient:
    """Bitget 선물 API 클라이언트"""
    
    def __init__(self):
        self.api_key = BITGET_API_KEY
        self.secret_key = BITGET_SECRET_KEY
        self.passphrase = BITGET_PASSPHRASE
        self.base_url = BITGET_BASE_URL
    
    def _generate_signature(self, timestamp: str, method: str, request_path: str, body: str = '') -> str:
        """API 서명 생성 - Bitget 공식 문서 기준"""
        # GET 요청에서 쿼리 파라미터가 있는 경우 request_path에 포함되어야 함
        message = timestamp + method.upper() + request_path + body
        
        # HMAC SHA256 서명 생성
        mac = hmac.new(
            bytes(self.secret_key, encoding='utf-8'),
            bytes(message, encoding='utf-8'),
            digestmod='sha256'
        )
        
        # Base64 인코딩
        signature = base64.b64encode(mac.digest()).decode()
        return signature
    
    def _make_request(self, method: str, endpoint: str, data: Dict = None) -> Dict:
        """API 요청 실행"""
        try:
            timestamp = str(int(time.time() * 1000))
            
            # 선물 거래 엔드포인트
            request_path = f"/api/mix/v1{endpoint}"
            
            # GET 요청의 경우 쿼리 파라미터를 URL에 추가
            if method.upper() == 'GET' and data:
                params = '&'.join([f"{k}={v}" for k, v in data.items()])
                full_path = f"{request_path}?{params}"
                body = ''
            else:
                full_path = request_path
                body = json.dumps(data) if data else ''
            
            # 서명 생성 (GET 요청은 쿼리 파라미터 포함된 경로 사용)
            signature = self._generate_signature(timestamp, method.upper(), full_path, body)
            
            headers = {
                'ACCESS-KEY': self.api_key,
                'ACCESS-SIGN': signature,
                'ACCESS-TIMESTAMP': timestamp,
                'ACCESS-PASSPHRASE': self.passphrase,
                'Content-Type': 'application/json',
                'locale': 'en-US'
            }
            
            url = self.base_url + full_path
            
            if method.upper() == 'GET':
                response = requests.get(url, headers=headers, timeout=10)
            elif method.upper() == 'POST':
                response = requests.post(url, headers=headers, data=body, timeout=10)
            else:
                raise ValueError(f"Unsupported method: {method}")
            
            # 응답 처리
            if response.status_code != 200:
                logger.error(f"HTTP Error {response.status_code}: {response.text}")
                raise Exception(f"HTTP Error {response.status_code}")
            
            result = response.json()
            
            # Bitget API 에러 체크
            if result.get('code') != '00000':
                error_msg = result.get('msg', 'Unknown error')
                logger.error(f"API Error: {error_msg}, Full response: {result}")
                raise Exception(f"API Error: {error_msg}")
            
            return result.get('data', {})
            
        except Exception as e:
            logger.error(f"Bitget API 요청 실패: {str(e)}")
            raise
    
    def get_account_info(self, symbol: str) -> Dict:
        """계좌 정보 조회"""
        try:
            # BTCUSDT -> BTCUSDT_UMCBL 형식으로 변환
            formatted_symbol = symbol.replace('USDT', 'USDT_UMCBL')
            
            result = self._make_request('GET', '/account/account', {
                'symbol': formatted_symbol,
                'marginCoin': 'USDT'
            })
            return result
            
        except Exception as e:
            logger.error(f"계좌 정보 조회 실패: {str(e)}")
            return {}
    
    def get_available_balance(self) -> float:
        """사용 가능한 USDT 잔고 조회"""
        try:
            # 선물 계좌 잔고 조회 (수정된 엔드포인트)
            result = self._make_request('GET', '/account/accounts', {
                'productType': 'umcbl'
            })
            
            # 응답이 리스트인 경우
            if isinstance(result, list):
                for account in result:
                    if account.get('marginCoin') == 'USDT':
                        # available이 없으면 crossMaxAvailable 확인
                        available = account.get('available') or account.get('crossMaxAvailable') or account.get('usdtEquity')
                        if available:
                            return float(available)
            # 응답이 딕셔너리인 경우
            elif isinstance(result, dict):
                # 직접 USDT 정보 확인
                if result.get('marginCoin') == 'USDT':
                    available = result.get('available') or result.get('crossMaxAvailable') or result.get('usdtEquity')
                    if available:
                        return float(available)
            
            # 다른 방법으로 시도 - 특정 심볼로 계좌 정보 조회
            try:
                account_info = self._make_request('GET', '/account/account', {
                    'symbol': 'BTCUSDT_UMCBL',
                    'marginCoin': 'USDT'
                })
                if account_info:
                    # crossMaxAvailable: 크로스 모드에서 사용 가능한 최대 금액
                    # available: 격리 모드에서 사용 가능한 금액
                    available = account_info.get('crossMaxAvailable') or account_info.get('available')
                    if available:
                        return float(available)
            except:
                pass
            
            return 0.0
            
        except Exception as e:
            logger.error(f"잔고 조회 실패: {str(e)}")
            return 0.0
    
    def get_positions(self, symbol: str = None) -> list:
        """현재 포지션 조회"""
        try:
            params = {'productType': 'umcbl'}
            if symbol:
                params['symbol'] = symbol.replace('USDT', 'USDT_UMCBL')
            
            result = self._make_request('GET', '/position/allPosition', params)
            return result
            
        except Exception as e:
            logger.error(f"포지션 조회 실패: {str(e)}")
            return []
    
    def place_limit_order(self, symbol: str, side: str, size: float, price: float, 
                         leverage: int, tp_price: float = None, sl_price: float = None) -> Optional[str]:
        """지정가 주문 실행 - 가격 정밀도 처리 추가"""
        try:
            formatted_symbol = symbol.replace('USDT', 'USDT_UMCBL')
            
            # 가격을 소수점 2자리로 반올림 (Bitget 요구사항)
            price = round(price, 2)
            if tp_price:
                tp_price = round(tp_price, 2)
            if sl_price:
                sl_price = round(sl_price, 2)
            
            data = {
                'symbol': formatted_symbol,
                'marginCoin': 'USDT',
                'side': 'open_long' if side.lower() == 'buy' else 'open_short',
                'orderType': 'limit',
                'price': str(price),
                'size': str(size),
                'leverage': str(leverage),
                'timeinforce': 'normal'
            }
            
            # TP/SL 설정
            if tp_price and sl_price:
                data['presetTakeProfitPrice'] = str(tp_price)
                data['presetStopLossPrice'] = str(sl_price)
            
            result = self._make_request('POST', '/order/placeOrder', data)
            return result.get('orderId')
            
        except Exception as e:
            logger.error(f"주문 실행 실패: {str(e)}")
            return None
    
    def close_all_positions(self, symbol: str) -> bool:
        """모든 포지션 종료"""
        try:
            formatted_symbol = symbol.replace('USDT', 'USDT_UMCBL')
            
            data = {
                'symbol': formatted_symbol,
                'marginCoin': 'USDT',
                'holdSide': 'long'  # 또는 'short'
            }
            
            result = self._make_request('POST', '/order/close-all-positions', data)
            return True
            
        except Exception as e:
            logger.error(f"포지션 종료 실패: {str(e)}")
            return False

def send_telegram_message(message: str) -> bool:
    """텔레그램으로 메시지 전송"""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = {
            'chat_id': TELEGRAM_CHAT_ID,
            'text': message,
            'parse_mode': 'HTML'
        }
        
        response = requests.post(url, data=data, timeout=10)
        return response.status_code == 200
        
    except Exception as e:
        logger.error(f"텔레그램 전송 오류: {str(e)}")
        return False

def calculate_leverage(entry_price: float, sl_price: float) -> int:
    """레버리지 계산"""
    risk_percent = abs((entry_price - sl_price) / entry_price) * 100
    if risk_percent == 0:
        return 1
    leverage = int(LOSS_RATIO / risk_percent)
    return max(1, min(leverage, MAX_LEVERAGE))

def calculate_position_size(balance: float, leverage: int) -> float:
    """포지션 크기 계산 (100% 사용)"""
    return balance * leverage

def execute_entry_trade(data: Dict) -> Dict:
    """진입 거래 실행"""
    global current_position
    
    try:
        with position_lock:
            # 현재 포지션 확인
            if current_position is not None:
                message = f"""⚠️ <b>거래 신호 무시</b>

이미 진행 중인 거래가 있습니다.
현재 포지션: {current_position.get('symbol')}
진입가: {current_position.get('entry_price'):,.2f}

새로운 신호는 무시됩니다."""
                send_telegram_message(message)
                return {'status': 'ignored', 'reason': 'active_position_exists'}
            
            bitget = BitgetFuturesClient()
            
            # 기존 포지션 재확인 (API로 확인)
            symbol = data.get('symbol', '')
            positions = bitget.get_positions(symbol)
            if positions and len(positions) > 0:
                message = "⚠️ Bitget에 이미 열린 포지션이 있습니다. 신호를 무시합니다."
                send_telegram_message(message)
                return {'status': 'ignored', 'reason': 'position_exists_on_exchange'}
            
            # 거래 파라미터 - 가격 정밀도 처리
            entry_price = round(float(data.get('price', 0)), 2)  # 소수점 2자리로 제한
            tp_price = round(float(data.get('tp', 0)), 2)        # 소수점 2자리로 제한
            sl_price = round(float(data.get('sl', 0)), 2)        # 소수점 2자리로 제한
            
            # 레버리지 계산
            leverage = calculate_leverage(entry_price, sl_price)
            
            # 레버리지가 31 이상이면 거래 중단
            if leverage > MAX_LEVERAGE:
                message = f"""❌ <b>거래 범위가 너무 작습니다</b>

📈 심볼: {symbol}
📊 계산된 레버리지: {leverage}x
⚠️ 최대 허용 레버리지: {MAX_LEVERAGE}x

거래 범위가 작아서 진입하지 않습니다.
레버리지 {leverage}로 계산되었습니다."""
                send_telegram_message(message)
                return {'status': 'rejected', 'reason': 'leverage_too_high', 'leverage': leverage}
            
            # 잔고 확인
            balance = bitget.get_available_balance()
            if balance < 10:
                raise Exception(f"잔고 부족: {balance:.2f} USDT")
            
            # 포지션 크기 계산 (100% 사용)
            position_value = balance  # 100% 사용
            position_size = (position_value * leverage) / entry_price
            position_size = round(position_size, 3)
            
            # 지정가 주문 실행
            order_id = bitget.place_limit_order(
                symbol=symbol,
                side='buy',
                size=position_size,
                price=entry_price,
                leverage=leverage,
                tp_price=tp_price,
                sl_price=sl_price
            )
            
            if not order_id:
                raise Exception("주문 실행 실패")
            
            # 포지션 정보 저장
            current_position = {
                'symbol': symbol,
                'entry_price': entry_price,
                'tp_price': tp_price,
                'sl_price': sl_price,
                'size': position_size,
                'leverage': leverage,
                'order_id': order_id,
                'timestamp': datetime.now().isoformat(),
                'balance_used': position_value
            }
            
            # 성공 메시지
            risk_amount = position_value * (LOSS_RATIO / 100)
            potential_profit = position_value * leverage * ((tp_price - entry_price) / entry_price)
            
            message = f"""✅ <b>거래 진입 완료!</b>

📈 <b>심볼:</b> {symbol}

💰 <b>진입가:</b> {entry_price:,.2f} USDT
🎯 <b>익절가:</b> {tp_price:,.2f} USDT (+{((tp_price-entry_price)/entry_price)*100:.2f}%)
🛑 <b>손절가:</b> {sl_price:,.2f} USDT ({((sl_price-entry_price)/entry_price)*100:.2f}%)

📊 <b>레버리지:</b> {leverage}x
💵 <b>사용 잔고:</b> {position_value:,.2f} USDT (100%)
📈 <b>포지션 크기:</b> {position_size:.3f} {symbol.replace('USDT', '')}

💎 <b>예상 수익:</b> +{potential_profit:,.2f} USDT
⚠️ <b>최대 손실:</b> -{risk_amount:,.2f} USDT ({LOSS_RATIO}%)

📋 <b>주문 ID:</b> {order_id}"""
            
            send_telegram_message(message)
            logger.info(f"거래 진입: {symbol} @ {entry_price}, 레버리지: {leverage}x")
            
            return {
                'status': 'success',
                'position': current_position
            }
            
    except Exception as e:
        error_message = f"""❌ <b>거래 실행 실패!</b>

📈 <b>심볼:</b> {data.get('symbol')}
⚠️ <b>오류:</b> {str(e)}
⏰ <b>시간:</b> {datetime.now().strftime("%H:%M:%S")}"""
        
        send_telegram_message(error_message)
        logger.error(f"거래 실행 실패: {str(e)}")
        
        return {
            'status': 'error',
            'message': str(e)
        }

def execute_exit_trade(data: Dict) -> Dict:
    """종료 신호 처리 (통계 기록용)
    
    주의: TP/SL은 이미 거래소에 설정되어 있으므로,
    이 함수는 통계 업데이트와 알림 전송만 담당합니다.
    실제 포지션 종료는 거래소가 자동으로 처리합니다.
    """
    global current_position, stats
    
    try:
        with position_lock:
            symbol = data.get('symbol', '')
            # exit_price도 소수점 2자리로 제한
            exit_price = round(float(data.get('exit_price', 0)), 2)
            result = data.get('result', '').upper()
            
            # 포지션 정보 확인
            if current_position and current_position.get('symbol') == symbol:
                entry_price = current_position['entry_price']
                leverage = current_position['leverage']
                balance_used = current_position['balance_used']
                
                # 수익률 계산
                price_change_percent = ((exit_price - entry_price) / entry_price) * 100
                profit_rate = price_change_percent * leverage
                profit_amount = balance_used * (profit_rate / 100)
                
                # 통계 업데이트
                if result == 'PROFIT' or exit_price >= current_position['tp_price']:
                    trade_result = 'WIN'
                    stats.add_trade('WIN', profit_rate, symbol)
                    emoji = "🎉"
                    result_text = "익절"
                else:
                    trade_result = 'LOSS'
                    stats.add_trade('LOSS', profit_rate, symbol)
                    emoji = "😔"
                    result_text = "손절"
                
                stats.save()
                
                # 메시지 전송
                message = f"""{emoji} <b>거래 종료 알림</b>

📈 <b>심볼:</b> {symbol}
🔥 <b>결과:</b> {result_text}

💰 <b>진입가:</b> {entry_price:,.2f} USDT
🎯 <b>종료가:</b> {exit_price:,.2f} USDT
📊 <b>가격 변동:</b> {price_change_percent:+.2f}%

🎰 <b>레버리지:</b> {leverage}x
💵 <b>투자금액:</b> {balance_used:,.2f} USDT
📈 <b>수익률:</b> {profit_rate:+.2f}%
💰 <b>손익:</b> {profit_amount:+,.2f} USDT

📊 <b>전체 통계</b>
✅ 익절: {stats.wins}회
❌ 손절: {stats.losses}회
📈 승률: {stats.get_win_rate():.1f}%

ℹ️ <i>주의: TP/SL은 거래소에서 자동 실행됩니다</i>"""
                
                send_telegram_message(message)
                logger.info(f"거래 종료: {symbol} - {result_text}, 수익률: {profit_rate:.2f}%")
                
                # 포지션 초기화
                current_position = None
                
                return {
                    'status': 'success',
                    'result': trade_result,
                    'profit_rate': profit_rate,
                    'profit_amount': profit_amount
                }
            else:
                message = f"""⚠️ <b>종료 신호 수신</b>

📈 심볼: {symbol}
🎯 종료가: {exit_price:,.2f}

활성 포지션이 없거나 심볼이 일치하지 않습니다."""
                send_telegram_message(message)
                
                return {
                    'status': 'warning',
                    'message': 'No matching position found'
                }
                
    except Exception as e:
        logger.error(f"종료 처리 실패: {str(e)}")
        return {
            'status': 'error',
            'message': str(e)
        }

# 텔레그램 명령어 처리를 위한 스레드
def telegram_bot_polling():
    """텔레그램 봇 폴링"""
    import time
    last_update_id = 0
    
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
            params = {'offset': last_update_id + 1, 'timeout': 30}
            response = requests.get(url, params=params, timeout=35)
            
            if response.status_code == 200:
                updates = response.json().get('result', [])
                
                for update in updates:
                    last_update_id = update['update_id']
                    
                    if 'message' in update and 'text' in update['message']:
                        text = update['message']['text']
                        chat_id = update['message']['chat']['id']
                        
                        if str(chat_id) == TELEGRAM_CHAT_ID:
                            handle_telegram_command(text)
            
            time.sleep(1)
            
        except Exception as e:
            logger.error(f"텔레그램 폴링 오류: {str(e)}")
            time.sleep(5)

def handle_telegram_command(command: str):
    """텔레그램 명령어 처리"""
    global stats
    
    try:
        if command == '/R' or command == '/r':
            # 통계 리셋
            stats.reset()
            stats.save()
            
            message = """🔄 <b>통계가 초기화되었습니다</b>

✅ 익절: 0회
❌ 손절: 0회
📈 승률: 0.0%

새로운 통계 수집을 시작합니다."""
            send_telegram_message(message)
            
        elif command == '/M' or command == '/m':
            # Bitget 서버 연결 상태 확인
            message = "🔍 <b>Bitget 서버 연결 확인 중...</b>"
            send_telegram_message(message)
            
            try:
                bitget = BitgetFuturesClient()
                start_time = time.time()
                
                # 1. API 연결 테스트 (계좌 정보 조회)
                balance = bitget.get_available_balance()
                api_latency = (time.time() - start_time) * 1000  # ms
                
                # 2. 더 상세한 계좌 정보 조회 시도
                detailed_balance_info = ""
                try:
                    # 전체 계좌 정보 조회
                    accounts_result = bitget._make_request('GET', '/account/accounts', {'productType': 'umcbl'})
                    if accounts_result:
                        if isinstance(accounts_result, list):
                            for acc in accounts_result:
                                if acc.get('marginCoin') == 'USDT':
                                    equity = acc.get('usdtEquity', 0)
                                    available = acc.get('available', 0)
                                    cross_available = acc.get('crossMaxAvailable', 0)
                                    frozen = acc.get('frozen', 0)
                                    unrealized_pnl = acc.get('unrealizedPL', 0)
                                    detailed_balance_info = f"""
💎 <b>계좌 상세:</b>
• 총 자산: {float(equity):,.2f} USDT
• 가용 잔고: {float(available):,.2f} USDT
• 크로스 가용: {float(cross_available):,.2f} USDT
• 동결 금액: {float(frozen):,.2f} USDT
• 미실현 손익: {float(unrealized_pnl):,.2f} USDT"""
                                    # 가장 큰 값을 실제 잔고로 사용
                                    balance = max(float(available), float(cross_available), float(equity))
                except Exception as e:
                    detailed_balance_info = f"\n⚠️ 상세 정보 조회 실패: {str(e)}"
                
                # 3. 서버 시간 확인 (Bitget 선물 API 사용)
                server_time_test = True
                time_sync = "확인 중..."
                try:
                    # Bitget 선물 공개 API로 서버 시간 확인
                    response = requests.get(
                        f"{BITGET_BASE_URL}/api/mix/v1/market/time",
                        timeout=5
                    )
                    
                    if response.status_code == 200:
                        server_data = response.json()
                        if server_data.get('code') == '00000':
                            server_timestamp = int(server_data.get('data', 0))
                            local_timestamp = int(time.time() * 1000)
                            time_diff = abs(server_timestamp - local_timestamp)
                            
                            if time_diff < 1000:
                                time_sync = f"✅ 완벽 동기화 ({time_diff}ms)"
                            elif time_diff < 5000:
                                time_sync = f"✅ 정상 ({time_diff}ms 차이)"
                            elif time_diff < 30000:
                                time_sync = f"⚠️ 약간 차이 ({time_diff}ms)"
                            else:
                                time_sync = f"❌ 큰 차이 ({time_diff/1000:.1f}초)"
                        else:
                            # 첫 번째 방법 실패 시 다른 엔드포인트 시도
                            response2 = requests.get(
                                f"{BITGET_BASE_URL}/api/spot/v1/public/time",
                                timeout=5
                            )
                            if response2.status_code == 200:
                                server_data2 = response2.json()
                                if server_data2.get('code') == '00000':
                                    server_timestamp = int(server_data2.get('data', {}).get('serverTime', 0))
                                    local_timestamp = int(time.time() * 1000)
                                    time_diff = abs(server_timestamp - local_timestamp)
                                    time_sync = f"정상 ({time_diff}ms 차이)" if time_diff < 5000 else f"차이 {time_diff}ms"
                                else:
                                    time_sync = "API 응답 오류"
                            else:
                                time_sync = "서버 접근 불가"
                    else:
                        # 시간 동기화를 로컬 시간으로만 표시
                        time_sync = f"로컬 시간 사용"
                        
                except Exception as e:
                    # 시간 동기화 실패해도 다른 기능은 정상 작동
                    server_time_test = False
                    time_sync = "확인 생략 (영향 없음)"
                    logger.debug(f"시간 동기화 확인 실패: {str(e)}")
                
                # 4. 포지션 조회 테스트
                positions_test = True
                positions_info = ""
                try:
                    positions = bitget.get_positions()
                    positions_count = len(positions) if positions else 0
                    if positions and len(positions) > 0:
                        positions_info = "\n📊 <b>활성 포지션:</b>"
                        for pos in positions[:3]:  # 최대 3개만 표시
                            symbol = pos.get('symbol', 'Unknown')
                            side = pos.get('holdSide', '')
                            size = pos.get('total', 0)
                            positions_info += f"\n• {symbol}: {side} {size}"
                except:
                    positions_test = False
                    positions_count = -1
                
                # 연결 상태 평가
                if api_latency < 3000:
                    status_emoji = "✅"
                    status_text = "정상"
                    status_detail = "모든 시스템 정상 작동"
                elif api_latency < 5000:
                    status_emoji = "⚠️"
                    status_text = "느림"
                    status_detail = f"응답 지연 ({api_latency:.0f}ms)"
                else:
                    status_emoji = "❌"
                    status_text = "매우 느림"
                    status_detail = f"심각한 지연 ({api_latency:.0f}ms)"
                
                # 상태 메시지 구성
                message = f"""{status_emoji} <b>Bitget 서버 상태</b>

📡 <b>연결 상태:</b> {status_text}
⚡ <b>응답 속도:</b> {api_latency:.0f}ms
🕐 <b>시간 동기화:</b> {time_sync}
{detailed_balance_info if detailed_balance_info else f'💵 <b>가용 잔고:</b> {balance:,.2f} USDT'}
📈 <b>포지션 수:</b> {positions_count if positions_count >= 0 else '확인 불가'}개{positions_info}

📝 <b>상태 요약:</b> {status_detail}
⏰ <b>확인 시간:</b> {datetime.now().strftime('%H:%M:%S')}

💡 <b>참고:</b> 선물 계좌 잔고를 표시합니다.
현물 계좌와는 별도로 관리됩니다."""
                
            except Exception as e:
                # 연결 실패 메시지
                message = f"""❌ <b>Bitget 서버 연결 실패</b>

⚠️ <b>오류 내용:</b> {str(e)}

<b>확인 사항:</b>
1. API Key가 올바른지 확인
2. Secret Key가 올바른지 확인
3. Passphrase가 올바른지 확인
4. API 권한 설정 확인 (Futures 권한)
5. IP 화이트리스트 설정 확인

⏰ <b>확인 시간:</b> {datetime.now().strftime('%H:%M:%S')}"""
            
            send_telegram_message(message)
            
        elif command == '/S' or command == '/s':
            # 통계 및 상태 조회
            bitget = BitgetFuturesClient()
            
            # 계좌 정보
            balance = bitget.get_available_balance()
            positions = bitget.get_positions()
            
            # 포지션 정보
            position_info = "없음"
            if current_position:
                position_info = f"{current_position['symbol']} (레버리지: {current_position['leverage']}x)"
            elif positions:
                position_info = f"{len(positions)}개 포지션 활성"
            
            # 최근 거래 내역
            recent_trades = ""
            if stats.trades_history:
                last_5_trades = stats.trades_history[-5:]
                for trade in reversed(last_5_trades):
                    emoji = "✅" if trade['result'] == 'WIN' else "❌"
                    recent_trades += f"\n{emoji} {trade['symbol']}: {trade['profit_rate']:+.2f}%"
            
            if not recent_trades:
                recent_trades = "\n최근 거래 없음"
            
            message = f"""📊 <b>거래 현황 및 통계</b>

💰 <b>계좌 정보</b>
• 가용 잔고: {balance:,.2f} USDT
• 거래 상태: {position_info}

📈 <b>거래 통계</b>
• 익절: {stats.wins}회
• 손절: {stats.losses}회
• 전체: {stats.total_trades}회
• 승률: {stats.get_win_rate():.1f}%

📋 <b>최근 거래 (최대 5개)</b>{recent_trades}

⏰ 통계 시작: {stats.start_date.strftime('%Y-%m-%d %H:%M')}"""
            
            send_telegram_message(message)
            
    except Exception as e:
        logger.error(f"명령어 처리 오류: {str(e)}")
        send_telegram_message(f"❌ 명령어 처리 중 오류 발생: {str(e)}")

# Flask 라우트
@app.route('/', methods=['GET'])
def home():
    """서버 상태 확인"""
    return jsonify({
        'status': 'healthy',
        'message': 'Bitget 자동거래 웹훅 서버 작동중',
        'time': datetime.now().isoformat(),
        'settings': {
            'loss_ratio': LOSS_RATIO,
            'max_leverage': MAX_LEVERAGE,
            'position_size': '100%'
        },
        'active_position': current_position is not None,
        'stats': {
            'wins': stats.wins,
            'losses': stats.losses,
            'win_rate': stats.get_win_rate()
        }
    })

@app.route('/webhook', methods=['POST'])
def webhook():
    """TradingView 웹훅 수신"""
    try:
        # Content-Type 확인 및 데이터 파싱
        content_type = request.headers.get('Content-Type', '')
        
        # JSON 데이터 파싱 시도
        if 'application/json' in content_type:
            data = request.get_json()
        else:
            # Content-Type이 application/json이 아닌 경우 raw data로 파싱
            raw_data = request.get_data(as_text=True)
            try:
                # TradingView는 때때로 text/plain으로 JSON을 보냄
                data = json.loads(raw_data)
            except json.JSONDecodeError:
                # JSON 파싱 실패 시 raw 텍스트 그대로 처리
                logger.warning(f"JSON 파싱 실패, raw data: {raw_data[:200]}")
                data = {'raw_message': raw_data}
        
        if not data:
            return jsonify({'error': 'No data received'}), 400
        
        logger.info(f"웹훅 수신: {data}")
        
        action = data.get('action', '').upper()
        
        if action == 'ENTRY':
            result = execute_entry_trade(data)
            return jsonify(result), 200 if result['status'] == 'success' else 400
            
        elif action == 'EXIT':
            result = execute_exit_trade(data)
            return jsonify(result), 200
            
        else:
            # action이 없는 경우 raw message 확인
            if 'raw_message' in data:
                logger.warning(f"알 수 없는 메시지 형식: {data['raw_message'][:100]}")
                message = f"""⚠️ <b>알 수 없는 웹훅 형식</b>

받은 데이터: {data['raw_message'][:200]}

TradingView Alert 메시지를 JSON 형식으로 설정해주세요:
{{"action": "ENTRY", "symbol": "BTCUSDT", ...}}"""
                send_telegram_message(message)
            
            return jsonify({'error': f'Unknown action: {action}'}), 400
            
    except Exception as e:
        logger.error(f"웹훅 처리 오류: {str(e)}")
        
        # 오류 상세 정보 텔레그램 전송
        error_message = f"""❌ <b>웹훅 처리 오류</b>

오류: {str(e)}
시간: {datetime.now().strftime('%H:%M:%S')}

TradingView Alert 설정을 확인해주세요."""
        send_telegram_message(error_message)
        
        return jsonify({'error': str(e)}), 500

@app.route('/test', methods=['GET'])
def test_connection():
    """연결 테스트"""
    try:
        bitget = BitgetFuturesClient()
        balance = bitget.get_available_balance()
        
        message = f"""🧪 <b>시스템 테스트</b>

✅ 서버: 정상
✅ Bitget API: 연결됨
💰 잔고: {balance:,.2f} USDT
📊 손실 비율: {LOSS_RATIO}%
🎰 최대 레버리지: {MAX_LEVERAGE}x

텔레그램 명령어:
/S - 상태 및 통계 조회
/R - 통계 초기화
/M - Bitget 서버 상태 확인"""
        
        send_telegram_message(message)
        
        return jsonify({
            'status': 'success',
            'balance': balance
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    # 텔레그램 봇 폴링 스레드 시작
    import threading
    bot_thread = threading.Thread(target=telegram_bot_polling, daemon=True)
    bot_thread.start()
    
    # Flask 서버 시작
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
