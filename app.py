# BitgetFuturesClient 클래스에 추가할 메서드
def set_leverage(self, symbol: str, leverage: int, hold_side: str = 'long') -> bool:
    """레버리지 설정"""
    try:
        formatted_symbol = symbol.replace('USDT', 'USDT_UMCBL')
        
        data = {
            'symbol': formatted_symbol,
            'marginCoin': 'USDT',
            'leverage': str(leverage),
            'holdSide': hold_side  # 'long' 또는 'short'
        }
        
        result = self._make_request('POST', '/account/setLeverage', data)
        logger.info(f"레버리지 설정 완료: {symbol} - {leverage}x")
        return True
        
    except Exception as e:
        logger.error(f"레버리지 설정 실패: {str(e)}")
        return False

# execute_entry_trade 함수 수정 부분
def execute_entry_trade(data: Dict) -> Dict:
    """진입 거래 실행"""
    global current_position
    
    try:
        with position_lock:
            bitget = BitgetFuturesClient()
            
            # API로 기존 포지션 확인
            symbol = data.get('symbol', '')
            positions = bitget.get_positions(symbol)
            if positions and len(positions) > 0:
                message = "⚠️ Bitget에 이미 열린 포지션이 있습니다. 신호를 무시합니다."
                send_telegram_message(message)
                return {'status': 'ignored', 'reason': 'position_exists_on_exchange'}
            
            # 거래 파라미터 - 가격 정밀도 처리
            entry_price = round(float(data.get('price', 0)), 2)
            tp_price = round(float(data.get('tp', 0)), 2)
            sl_price = round(float(data.get('sl', 0)), 2)
            
            # 레버리지 계산
            leverage = calculate_leverage(entry_price, sl_price)
            
            # 레버리지가 31 이상이면 거래 중단
            if leverage > MAX_LEVERAGE:
                message = f"""❌ <b>거래 범위가 너무 작습니다</b>

📈 심볼: {symbol}
📊 계산된 레버리지: {leverage}x
⚠️ 최대 허용 레버리지: {MAX_LEVERAGE}x

거래 범위가 작아서 진입하지 않습니다."""
                send_telegram_message(message)
                return {'status': 'rejected', 'reason': 'leverage_too_high', 'leverage': leverage}
            
            # 잔고 확인
            balance = bitget.get_available_balance()
            if balance < 10:
                raise Exception(f"잔고 부족: {balance:.2f} USDT")
            
            # ===== 중요: 레버리지 먼저 설정 =====
            if not bitget.set_leverage(symbol, leverage):
                raise Exception(f"레버리지 설정 실패: {leverage}x")
            
            # 포지션 크기 계산 - 안전 마진 적용
            position_value = balance * 0.95  # 95%만 사용
            position_notional = position_value * leverage  # 명목상 포지션 크기
            position_size = position_notional / entry_price  # 실제 코인 수량
            position_size = round(position_size, 3)
            
            # 최소 주문 크기 확인
            if position_size < 0.001:
                raise Exception(f"포지션 크기가 너무 작습니다: {position_size:.6f}")
                
            logger.info(f"포지션 계산: 잔고={balance:.2f}, 레버리지={leverage}x, 포지션크기={position_size:.3f}")
            
            # 지정가 주문 실행 (레버리지는 이미 설정되었으므로 주문에서는 제외 가능)
            order_id = bitget.place_limit_order(
                symbol=symbol,
                side='buy',
                size=position_size,
                price=entry_price,
                leverage=leverage,  # 참고용으로 전달하지만 실제로는 이미 설정됨
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

📊 <b>레버리지:</b> {leverage}x (설정 완료)
💵 <b>사용 잔고:</b> {position_value:,.2f} USDT (95%)
💵 <b>전체 잔고:</b> {balance:,.2f} USDT
📈 <b>포지션 크기:</b> {position_size:.3f} {symbol.replace('USDT', '')}

💎 <b>예상 수익:</b> +{potential_profit:,.2f} USDT
⚠️ <b>최대 손실:</b> -{risk_amount:,.2f} USDT ({LOSS_RATIO}%)

📋 <b>주문 ID:</b> {order_id}"""
            
            send_telegram_message(message)
            logger.info(f"거래 진입: {symbol} @ {entry_price}, 레버리지: {leverage}x (설정 완료)")
            
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
