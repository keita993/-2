import streamlit as st
import yfinance as yf
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from datetime import datetime, timedelta
import talib as ta
import sqlite3
import jwt
import os
from dotenv import load_dotenv

# 環境変数の読み込み
load_dotenv()

# ページ設定
st.set_page_config(
    page_title="株式期待値分析ツール",
    page_icon="��",
    layout="wide",
    initial_sidebar_state="expanded"
)

# キャッシュ設定
@st.cache_data(ttl=3600)
def get_db():
    conn = sqlite3.connect('users.db')
    conn.row_factory = sqlite3.Row
    return conn

# ユーザー認証関連の関数
def create_user(username, password):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('INSERT INTO users (username, password_hash) VALUES (?, ?)',
                  (username, password))
    conn.commit()
    conn.close()

def get_user(username):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM users WHERE username = ?', (username,))
    user = cursor.fetchone()
    conn.close()
    return user

def create_token(user_id):
    payload = {
        'user': user_id,
        'exp': datetime.utcnow() + timedelta(days=1)
    }
    return jwt.encode(payload, os.getenv('FLASK_SECRET_KEY', 'supersecretkey'), algorithm='HS256')

# テクニカル分析関連の関数
def calculate_rsi(data, period=14):
    return ta.RSI(data['Close'], timeperiod=period)

def calculate_bollinger_bands(data, period=20, num_std=3):
    upper, middle, lower = ta.BBANDS(data['Close'], timeperiod=period, nbdevup=num_std, nbdevdn=num_std)
    deviation_upper = (upper - middle) / middle * 100
    deviation_lower = (lower - middle) / middle * 100
    return upper, lower, deviation_upper, deviation_lower

def calculate_sdi(data, period=20):
    """上昇期待値指数（SDI）を計算する"""
    rsi = calculate_rsi(data)
    _, _, _, deviation_lower = calculate_bollinger_bands(data)
    
    sdi = pd.Series(index=data.index, dtype=float)
    for i in range(len(data)):
        if pd.isna(rsi.iloc[i]) or pd.isna(deviation_lower.iloc[i]):
            sdi.iloc[i] = None
        else:
            rsi_component = 50 - rsi.iloc[i]
            deviation_component = abs(min(0, deviation_lower.iloc[i]))
            raw_expectation = rsi_component + deviation_component
            sdi.iloc[i] = raw_expectation * 2 if raw_expectation >= 0 else raw_expectation
    
    return sdi

def get_stock_data(ticker, period):
    try:
        normalized_ticker = normalize_ticker(ticker)
        stock = yf.Ticker(normalized_ticker)
        data = stock.history(period=period)
        if len(data) == 0:
            st.error(f"データが取得できませんでした: {ticker}")
            return None
        return data
    except Exception as e:
        st.error(f"データの取得に失敗しました: {str(e)}")
        return None

def normalize_ticker(ticker):
    if ticker.isdigit():
        return f"{ticker}.T"
    return ticker

def get_stock_name(ticker):
    """銘柄名を取得する"""
    ticker_to_fetch = normalize_ticker(ticker)
    stock = yf.Ticker(ticker_to_fetch)
    try:
        stock_info = stock.info
        stock_name = stock_info.get('longName', '') or stock_info.get('shortName', '') or ticker_to_fetch
        if '.T' in ticker_to_fetch:
            stock_name = f"{stock_name} ({ticker_to_fetch.replace('.T', '')})"
        else:
            stock_name = f"{stock_name} ({ticker_to_fetch})"
        return stock_name, stock_info
    except:
        return ticker_to_fetch, None

def calculate_backtest(data, buy_threshold=30, sell_threshold=0, disable_sell=False, shares=100):
    """バックテストを実行する"""
    if data is None or len(data) == 0:
        return None
    
    # SDIの計算
    data['SDI'] = calculate_sdi(data)
    
    # バックテストの実行
    trades = []
    positions = []  # 複数のポジションを管理するリスト
    latest_price = data['Close'].iloc[-1]  # 最新の株価
    latest_date = data.index[-1]  # 最新の日付
    
    # データを古い順に処理
    for i in range(len(data)):
        current_date = data.index[i]
        current_price = data['Close'].iloc[i]
        current_sdi = data['SDI'].iloc[i]
        
        # 買いシグナルの確認
        if current_sdi is not None and current_sdi >= buy_threshold:
            # 新しいポジションを追加
            position = {
                'buy_date': current_date,
                'buy_price': current_price,
                'shares': shares,
                'buy_expectation': current_sdi,
                'buy_date_obj': current_date
            }
            positions.append(position)
            
            # 売りなしの場合は即座に取引を記録
            if disable_sell:
                trade = {
                    'buy_date': current_date,
                    'sell_date': latest_date,
                    'buy_price': current_price,
                    'sell_price': latest_price,
                    'shares': shares,
                    'buy_expectation': current_sdi,
                    'sell_expectation': None,
                    'profit_rate': ((latest_price - current_price) / current_price) * 100,
                    'profit_amount': (latest_price - current_price) * shares
                }
                trades.append(trade)
        
        # 売りシグナルの確認（売りなしでない場合のみ）
        elif not disable_sell and sell_threshold is not None and \
             current_sdi is not None and current_sdi <= sell_threshold and \
             len(positions) > 0:
            # 現在保有している全ポジションを処理
            for position in positions:
                # 買付日より後の場合のみ売却
                if current_date > position['buy_date_obj']:
                    trade = {
                        'buy_date': position['buy_date'],
                        'sell_date': current_date,
                        'buy_price': position['buy_price'],
                        'sell_price': current_price,
                        'shares': position['shares'],
                        'buy_expectation': position['buy_expectation'],
                        'sell_expectation': current_sdi,
                        'profit_rate': ((current_price - position['buy_price']) / position['buy_price']) * 100,
                        'profit_amount': (current_price - position['buy_price']) * position['shares']
                    }
                    trades.append(trade)
            # ポジションをクリア
            positions = []
    
    # 最終ポジションの処理（売りなしでない場合のみ）
    if len(positions) > 0 and not disable_sell:
        for position in positions:
            if latest_date > position['buy_date_obj']:
                trade = {
                    'buy_date': position['buy_date'],
                    'sell_date': latest_date,
                    'buy_price': position['buy_price'],
                    'sell_price': latest_price,
                    'shares': position['shares'],
                    'buy_expectation': position['buy_expectation'],
                    'sell_expectation': data['SDI'].iloc[-1],
                    'profit_rate': ((latest_price - position['buy_price']) / position['buy_price']) * 100,
                    'profit_amount': (latest_price - position['buy_price']) * position['shares']
                }
                trades.append(trade)
    
    # パフォーマンス指標の計算
    total_trades = len(trades)
    if total_trades > 0:
        winning_trades = len([t for t in trades if t['profit_rate'] > 0])
        losing_trades = total_trades - winning_trades
        total_profit_amount = sum(t['profit_amount'] for t in trades)
        average_profit_rate = sum(t['profit_rate'] for t in trades) / total_trades
        
        # 売却日ごとの利益率を平均化
        sell_date_profits = {}
        for trade in trades:
            sell_date = trade['sell_date']
            if sell_date not in sell_date_profits:
                sell_date_profits[sell_date] = []
            sell_date_profits[sell_date].append(trade['profit_rate'])
        
        # 各売却日の平均利益率を計算
        avg_daily_profits = [sum(profits) / len(profits) for profits in sell_date_profits.values()]
        
        # 複利での総利益率の計算
        total_profit_rate = (np.prod([1 + r/100 for r in avg_daily_profits]) - 1) * 100
        
        # 売りなしの場合は総利益率を平均利益率にする
        if disable_sell:
            total_profit_rate = average_profit_rate
    else:
        winning_trades = 0
        losing_trades = 0
        total_profit_amount = 0
        total_profit_rate = 0
        average_profit_rate = 0
    
    return {
        'trades': trades,
        'performance': {
            'total_trades': total_trades,
            'winning_trades': winning_trades,
            'losing_trades': losing_trades,
            'win_rate': (winning_trades / total_trades * 100) if total_trades > 0 else 0,
            'total_profit_rate': total_profit_rate,
            'total_profit_amount': total_profit_amount,
            'average_profit_rate': average_profit_rate
        }
    }

# メインインターフェース
st.title("株式期待値分析ツール")

# セッション状態の初期化
if 'user' not in st.session_state:
    st.session_state.user = None
if 'stock_data' not in st.session_state:
    st.session_state.stock_data = None
if 'backtest_results' not in st.session_state:
    st.session_state.backtest_results = None

# 認証状態に応じて表示を切り替え
if st.session_state.user is None:
    # ログインフォーム
    st.sidebar.header("ログイン")
    username = st.sidebar.text_input("ユーザー名")
    password = st.sidebar.text_input("パスワード", type="password")
    
    if st.sidebar.button("ログイン"):
        user = get_user(username)
        if user and user['password_hash'] == password:
            st.session_state.user = user
            st.success("ログイン成功")
            st.rerun()
        else:
            st.error("ユーザー名またはパスワードが間違っています")
    
    # 新規登録フォーム
    st.sidebar.header("新規登録")
    new_username = st.sidebar.text_input("新しいユーザー名")
    new_password = st.sidebar.text_input("新しいパスワード", type="password")
    
    if st.sidebar.button("登録"):
        if get_user(new_username):
            st.error("このユーザー名は既に使用されています")
        else:
            create_user(new_username, new_password)
            st.success("登録が完了しました")
else:
    # メインコンテンツ
    st.sidebar.header("設定")
    ticker = st.sidebar.text_input("銘柄コード（日本株は4桁の数字、米国株はティッカーシンボル）", "7203")
    period = st.sidebar.selectbox("期間", ["1mo", "3mo", "6mo", "1y", "2y", "5y", "10y"])
    
    # ポートフォリオ管理
    st.sidebar.header("ポートフォリオ")
    if st.sidebar.button("ポートフォリオに追加"):
        if ticker not in st.session_state.stock_data:
            st.session_state.stock_data[ticker] = get_stock_data(ticker, "1d")
            if st.session_state.stock_data[ticker] is not None:
                stock_name, stock_info = get_stock_name(ticker)
                latest_price = st.session_state.stock_data[ticker]['Close'].iloc[-1]
                sdi = calculate_sdi(st.session_state.stock_data[ticker]).iloc[-1]
                st.session_state.backtest_results[ticker] = {
                    'name': stock_name,
                    'price': latest_price,
                    'sdi': sdi,
                    'info': stock_info
                }
            st.success(f"{ticker}をポートフォリオに追加しました")
    
    if st.session_state.stock_data:
        st.sidebar.write("保有銘柄:")
        for p in st.session_state.stock_data:
            col1, col2, col3 = st.sidebar.columns([2, 2, 1])
            col1.write(p)
            if p in st.session_state.backtest_results:
                data = st.session_state.backtest_results[p]
                col2.write(f"¥{data['price']:,.0f}")
            if col3.button("削除", key=f"del_{p}"):
                st.session_state.stock_data.pop(p)
                st.session_state.backtest_results.pop(p)
                st.rerun()
    
    # メインコンテンツ
    if ticker:
        data = st.session_state.stock_data[ticker]
        if data is not None and len(data) > 0:
            # 株価チャート
            fig = go.Figure()
            fig.add_trace(go.Candlestick(
                x=data.index,
                open=data['Open'],
                high=data['High'],
                low=data['Low'],
                close=data['Close'],
                name='OHLC'
            ))
            
            # ボリンジャーバンドの計算と表示
            upper, lower, _, _ = calculate_bollinger_bands(data)
            fig.add_trace(go.Scatter(
                x=data.index,
                y=upper,
                name='上限バンド',
                line=dict(color='rgba(173, 204, 255, 0.2)')
            ))
            fig.add_trace(go.Scatter(
                x=data.index,
                y=lower,
                name='下限バンド',
                line=dict(color='rgba(173, 204, 255, 0.2)'),
                fill='tonexty'
            ))
            
            # RSIの計算と表示
            data['RSI'] = calculate_rsi(data)
            fig_rsi = go.Figure()
            fig_rsi.add_trace(go.Scatter(
                x=data.index,
                y=data['RSI'],
                name='RSI'
            ))
            
            # SDIの計算と表示
            data['SDI'] = calculate_sdi(data)
            fig_sdi = go.Figure()
            fig_sdi.add_trace(go.Scatter(
                x=data.index,
                y=data['SDI'],
                name='SDI（上昇期待値指数）'
            ))
            
            # チャートの表示
            st.plotly_chart(fig, use_container_width=True)
            st.plotly_chart(fig_rsi, use_container_width=True)
            st.plotly_chart(fig_sdi, use_container_width=True)
            
            # バックテストの設定
            st.header("バックテスト")
            col1, col2, col3, col4 = st.columns(4)
            with col1:
                buy_threshold = st.number_input("買いシグナルSDI", 0, 100, 30)
            with col2:
                sell_threshold = st.number_input("売りシグナルSDI", -100, 100, 0)
            with col3:
                shares = st.number_input("取引株数", 1, 1000, 100)
            with col4:
                disable_sell = st.checkbox("売りを無効化")
            
            if st.button("バックテスト実行"):
                result = calculate_backtest(data, buy_threshold, sell_threshold, disable_sell, shares)
                if result:
                    st.subheader("取引履歴")
                    trades_df = pd.DataFrame(result['trades'])
                    if len(trades_df) > 0:
                        trades_df['buy_date'] = pd.to_datetime(trades_df['buy_date'])
                        trades_df['sell_date'] = pd.to_datetime(trades_df['sell_date'])
                        trades_df = trades_df.sort_values('buy_date', ascending=False)
                        st.dataframe(trades_df)
                    
                    st.subheader("パフォーマンス")
                    perf = result['performance']
                    col1, col2, col3, col4 = st.columns(4)
                    col1.metric("総取引回数", perf['total_trades'])
                    col2.metric("勝率", f"{perf['win_rate']:.1f}%")
                    col3.metric("平均利益率", f"{perf['average_profit_rate']:.1f}%")
                    col4.metric("総利益", f"¥{perf['total_profit_amount']:,.0f}")
    
    # ログアウトボタン
    if st.sidebar.button("ログアウト"):
        st.session_state.user = None
        st.session_state.stock_data = {}
        st.session_state.backtest_results = {}
        st.rerun() 