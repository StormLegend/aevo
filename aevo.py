import asyncio
import json
import random
import time
import traceback
import os
import requests
import websockets
from eth_account import Account
from eth_hash.auto import keccak
from loguru import logger
from web3 import Web3
import pickle
from collections import deque

from eip712_structs import Address, Boolean, EIP712Struct, Uint, make_domain


CONFIG = {
    "testnet": {
        "rest_url": "https://api-testnet.aevo.xyz",
        "ws_url": "wss://ws-testnet.aevo.xyz",
        "signing_domain": {
            "name": "Aevo Testnet",
            "version": "1",
            "chainId": "11155111",
        },
    },
    "mainnet": {
        "rest_url": "https://api.aevo.xyz",
        "ws_url": "wss://ws.aevo.xyz",
        "signing_domain": {
            "name": "Aevo Mainnet",
            "version": "1",
            "chainId": "1",
        },
    },
}


class Order(EIP712Struct):
    maker = Address()
    isBuy = Boolean()
    limitPrice = Uint(256)
    amount = Uint(256)
    salt = Uint(256)
    instrument = Uint(256)
    timestamp = Uint(256)


class AevoClient:
    def __init__(
        self,
        signing_key="",
        wallet_address="",
        api_key="",
        api_secret="",
        env="testnet",
        rest_headers={},
    ):
        self.signing_key = signing_key
        self.wallet_address = wallet_address
        self.api_key = api_key
        self.api_secret = api_secret
        self.client = requests
        self.connection = None
        self.orders = [] # 用于存储订单信息
        self.tickers = {} # 用于存储行情信息，以资产名称为键
        self.latest_tickers = deque(maxlen=100)
        self.subscribed_channels = []  # 新增：用于存储当前订阅的频道
        self.rest_headers = {
            "AEVO-KEY": api_key,
            "AEVO-SECRET": api_secret,
        }
        self.extra_headers = None
        self.rest_headers.update(rest_headers)
        if (env != "testnet") and (env != "mainnet"):
            raise ValueError("env must either be 'testnet' or 'mainnet'")
        self.env = env


    @property
    def address(self):
        return Account.from_key(self.signing_key).address

    @property
    def rest_url(self):
        return CONFIG[self.env]["rest_url"]

    @property
    def ws_url(self):
        return CONFIG[self.env]["ws_url"]

    @property
    def signing_domain(self):
        return CONFIG[self.env]["signing_domain"]



    async def process_updates(self):
        while True:
            price_update = await self.queue.get()  # Wait for a price update from the queue
            print(f"Index Price Update: {price_update}")

    async def open_connection(self, extra_headers={}):
        try:
            logger.info("Opening Aevo websocket connection...")

            # 如果当前已有连接，则关闭它
            if self.connection is not None:
                await self.connection.close()

            # 建立新的连接
            self.connection = await websockets.connect(
                self.ws_url, ping_interval=None, extra_headers=extra_headers
            )
            logger.debug(f"Connected to {self.ws_url}...")

            if self.api_key and self.wallet_address:
                # 发送认证信息
                await self.authenticate()

            logger.info("Aevo websocket connection established.")
        except Exception as e:
            logger.error("Error occurred while opening websocket connection.")
            logger.error(e)
            logger.error(traceback.format_exc())
            await asyncio.sleep(10)  # 避免立即重试

    async def authenticate(self):
        logger.debug("Authenticating...")
        await self.connection.send(
            json.dumps(
                {
                    "id": 1,
                    "op": "auth",
                    "data": {
                        "key": self.api_key,
                        "secret": self.api_secret,
                    },
                }
            )
        )
        # Sleep as authentication takes some time, especially slower on testnet
        await asyncio.sleep(1)

    async def start_heartbeat(self):
        # 定义心跳间隔时间，例如每30秒发送一次心跳
        heartbeat_interval = 30
        while True:
            await self.ping_server()
            await asyncio.sleep(heartbeat_interval)

    async def reconnect(self):
        logger.info("尝试重新连接WebSocket...")
        await self.close_connection()  # 确保现有的连接已经关闭

        # 在重新连接之前添加适当的延迟
        await asyncio.sleep(5)

        # 打开新的连接
        try:
            await self.open_connection(self.extra_headers)  # 如果有必要，传递任何额外的头部信息
            await self.resubscribe_channels()  # 重新订阅之前订阅的频道
            # 在这里不要启动 message_handler，它应该由主函数管理
        except Exception as e:
            logger.error(f"重连失败: {e}")
            # 如果重连失败，你可能需要实现一个重试机制或者抛出异常

    async def start(self, trade_asset):
        await self.open_connection(self.extra_headers)  # 确保传递额外的headers
        await self.subscribe_orders()
        await self.subscribe_ticker(f"{trade_asset}-PERP")  # 替换为你的资产名称
        await self.message_handler()  # 启动统一的消息处理循环
    async def resubscribe_channels(self):
        for channel in self.subscribed_channels:
            subscription_message = {
                "op": "subscribe",
                "data": [channel]
            }
            await self.connection.send(json.dumps(subscription_message))
            logger.info(f"重新订阅频道：{channel}")



    async def ping_server(self):
        if self.connection and not self.connection.closed:
            try:
                await self.connection.ping()
                logger.debug("向服务器发送心跳。")
            except Exception as e:
                logger.error(f"发送心跳时出错: {e}")
                await self.reconnect()

    async def close_connection(self):
        if self.connection and not self.connection.closed:
            logger.info("关闭现有的WebSocket连接...")
            try:
                await self.connection.close()
            except Exception as e:
                logger.error(f"关闭连接时出错: {e}")
            finally:
                self.connection = None
        else:
            logger.info("没有要关闭的活跃WebSocket连接。")

    # ...

    async def send(self, data):
        if self.connection is not None:
            try:
                await self.connection.send(data)
            except websockets.exceptions.ConnectionClosedError as e:
                logger.debug("Restarted Aevo websocket connection")
                await self.reconnect()
                await self.connection.send(data)  # 再次尝试发送数据
            except:
                await self.reconnect()  # 如果有其他异常，尝试重连
        else:
            logger.error("WebSocket connection is not established. Cannot send data.")
            await self.reconnect()  # 如果连接为 None，则尝试重连


    async def connect(self):
        self.connection = await websockets.connect(self.ws_url)
        logger.info("Connected to the WebSocket server.")

    async def message_handler(self,grid_interval,price_decimals):
        grid_interval = int(grid_interval)
        while True:
            try:
                response = await asyncio.wait_for(self.connection.recv(), timeout=20)
                # logger.debug(f"收到响应: {response}")  # 记录原始响应内容
                message = json.loads(response)
                # logger.debug(f"解码后的消息: {message}")  # 记录解码后的消息

                # 分发消息到对应的处理函数
                channel = message.get('channel')
                if channel and channel.startswith("ticker:"):
                    await self.handle_ticker_message(message)
                elif channel == "orders":
                    await self.handle_order_message(message)
                elif channel == "fills":
                    await self.handle_fills_message(message,grid_interval,price_decimals)  # 处理成交记录的消息
                    # logger.debug(f"{channel}解码后的消息: {message}")  # 记录解码后的消息
                else:
                    pass  # 处理其他类型的消息或忽略
            except asyncio.TimeoutError:
                logger.error("没有新的订单成交，等待中。")
                # 可以在这里添加重连逻辑
            except websockets.ConnectionClosedError:
                logger.error("WebSocket连接关闭。尝试重新连接...")
                await self.reconnect()  # 自定义的重连方法
            except json.JSONDecodeError as e:
                logger.error(f"解析JSON响应时出错: {e}")
            except Exception as e:
                logger.error(f"处理消息时发生错误: {e}")

    async def handle_fills_message(self, message,grid_interval,price_decimals):
        """处理成交记录消息并创建对侧订单。"""
        fill_data = message.get("data", {}).get("fill")
        if not fill_data:
            logger.error("收到的成交记录消息缺失 'fill' 数据。")
            return

        logger.info(f"成交记录: {fill_data}")

        # 提取成交记录信息
        order_id = fill_data.get("order_id")
        price_str = fill_data.get("price")
        filled_str = fill_data.get("filled")  # 注意这里不是 'quantity' 而是 'filled'
        side = fill_data.get("side")
        instrument_id = fill_data.get("instrument_id")

        # 确保所有必要的信息都存在且有效
        if not all([order_id, price_str, filled_str, side, instrument_id]):
            logger.error("成交记录消息缺失必要的数据。")
            return

        # 安全地进行类型转换
        try:
            filled_price = float(price_str)
            filled_quantity = float(filled_str)
            is_buy = side.lower() == "buy"
            instrument_id = int(instrument_id)
        except ValueError as e:
            logger.error(f"提取成交记录数据时出现类型转换错误: {e}")
            return

        # 计算对侧订单的价格和方向
        new_order_is_buy = not is_buy

        # 这里假设 grid_interval 和 price_decimals 是已定义的变量
        new_order_price = self.calculate_opposite_price(
            filled_price, new_order_is_buy, grid_interval, price_decimals
        )

        # 尝试创建对侧订单
        try:
            new_order_id = await self.create_order(
                # 假设aevo实例就是self
                instrument_id,
                new_order_is_buy,
                new_order_price,
                filled_quantity,
                True  # post_only设置为True
            )
            logger.info(f"对侧新订单已创建: {new_order_id}")
        except Exception as e:
            logger.error(f"创建对侧新订单时发生错误: {e}")

    def calculate_opposite_price(self, filled_price, is_buy, grid_interval, price_decimals):
        """根据成交订单的价格和方向计算对侧订单的价格。"""
        if is_buy:
            # 如果成交的是买单，计算对侧卖单的价格
            opposite_price = filled_price - grid_interval
        else:
            # 如果成交的是卖单，计算对侧买单的价格
            opposite_price = filled_price + grid_interval

        # 四舍五入到指定的价格小数位数
        return round(opposite_price, price_decimals)

    # 提供一个方法来获取当前的订单和行情信息
    def get_orders(self):
        return self.orders

    def get_tickers(self):
        return self.tickers
    async def handle_order_message(self, message):
        # 提取订单数据
        orders_data = message.get("data", {}).get("orders", [])

        # 遍历所有订单并处理
        for order in orders_data:
            # 打印订单信息
            logger.info(f"订单更新: {order}")
            # 在这里可以添加你的业务逻辑来处理订单
            # 例如，更新订单数据库，通知用户等
            # 因为这里是示例，我们只是简单地将订单信息添加到列表中
            self.orders.append(order)
        # 将更新后的订单信息保存到本地文件
        self.save_to_file(self.orders, 'data/orders.pkl')

    async def handle_ticker_message(self, message):
        # 提取并存储行情信息
        ticker_data = message.get("data", {})
        asset = message['channel'].split(':')[-1]  # 获取资产名称
        self.tickers[asset] = ticker_data
        # 将最新的行情数据添加到deque中，保持最新的100个数据
        self.latest_tickers.append(ticker_data)
        # 保存最新的100个行情数据到本地文件
        self.save_to_file(list(self.latest_tickers), f'data/{asset}_tickers.pkl')

    def save_to_file(self, data, filename):
        # 确保目录存在
        os.makedirs(os.path.dirname(filename), exist_ok=True)
        # 保存数据到文件
        with open(filename, 'wb') as file:
            pickle.dump(data, file)

    async def subscribe_ticker(self, asset):
        channel_name = f"ticker:{asset}"
        subscription_message = {
            "op": "subscribe",
            "data": [channel_name]
        }
        await self.connection.send(json.dumps(subscription_message))
        self.subscribed_channels.append(channel_name)  # 记录订阅的频道

        # 等待订阅确认
        try:
            confirmation = await asyncio.wait_for(self.connection.recv(), timeout=5)
            confirmation_message = json.loads(confirmation)

            if 'id' in confirmation_message and confirmation_message.get('data', {}).get('success'):
                logger.info(f"行情订阅确认成功: {channel_name}")
            else:
                logger.error(f"行情订阅失败: {channel_name} - {confirmation_message}")
                # 可以在这里添加重试逻辑或抛出异常

        except asyncio.TimeoutError:
            logger.error(f"Timeout while waiting for subscription confirmation: {channel_name}")
            # 可以在这里添加重试逻辑或抛出异常

    async def monitor_tickers(self):
        while True:
            try:
                response = await asyncio.wait_for(self.connection.recv(), timeout=30)
                message = json.loads(response)

                channel = message.get('channel')
                if channel and channel.startswith("ticker:"):
                    ticker_data = message.get("data", {}).get("tickers", [])
                    for ticker in ticker_data:
                        # 注释掉或移除下面的日志记录行，以避免在日志中显示行情信息
                        logger.info(f"收到行情信息: {ticker}")
                        # 在这里可以添加处理行情信息的逻辑
                elif 'id' in message and message.get('data', {}).get('success'):
                    # 处理订阅确认消息
                    logger.info("行情订阅确认成功。")  # 如果不需要此日志，也可以注释掉
                else:
                    # 如果接收到的消息不是行情更新，也不是订阅确认，则记录警告
                    logger.warning(f"在行情监控频道收到了非行情信息: {message}")

            except asyncio.TimeoutError:
                logger.error("订阅行情时等待服务器响应超时。")
            except json.JSONDecodeError as e:
                logger.error(f"解析JSON响应时出错: {e}")
            except Exception as e:
                logger.error(f"订阅行情时发生错误: {e}")


    async def subscribe_orders(self):
        channel_name = "orders"
        subscription_message = {
            "op": "subscribe",
            "data": [channel_name]
        }
        await self.connection.send(json.dumps(subscription_message))
        self.subscribed_channels.append(channel_name)  # 记录订阅的频道
        logger.info("已发送订单更新订阅请求。")


    async def start_message_dispatcher(self):
        while True:
            try:
                message = await asyncio.wait_for(self.connection.recv(), timeout=20)
                await self.handle_message(json.loads(message))
            except asyncio.TimeoutError:
                logger.error("等待服务器响应超时。")
                # 可以在这里添加重连逻辑
            except websockets.ConnectionClosedError:
                logger.error("WebSocket连接关闭。")
                break  # 或重新连接
            except json.JSONDecodeError as e:
                logger.error(f"解析JSON响应时出错: {e}")
            except Exception as e:
                logger.error(f"处理消息时发生错误: {e}")

    async def handle_message(self, message):
        channel = message.get('channel')
        if channel and channel.startswith("ticker:"):
            await self.handle_ticker_message(message)
        elif channel == "orders":
            await self.handle_order_message(message)
        elif channel == "fills":
            await self.handle_fills_message(message)  # 处理成交记录的消息
        else:
            # 处理其他类型的消息
            pass
    async def subscribe_fills(self):
        # 构建订阅成交记录的消息
        subscription_message = {
            "op": "subscribe",
            "data": ["fills"]
        }
        # 发送订阅消息到 WebSocket 服务器
        await self.connection.send(json.dumps(subscription_message))
        self.subscribed_channels.append("fills")  # 记录订阅的频道
        logger.info("已发送成交记录订阅请求。")

    # async def subscribe_fills(self):
    #     payload = {
    #         "op": "subscribe",
    #         "data": ["fills"],
    #     }
    #     await self.send(json.dumps(payload))


    # Public REST API
    def get_index(self, asset):
        req = self.client.get(f"{self.rest_url}/index?symbol={asset}")
        data = req.json()
        return data

    def get_markets(self, asset):
        req = self.client.get(f"{self.rest_url}/markets?asset={asset}")
        data = req.json()
        return data

    # Private REST API
    def rest_create_order(
        self, instrument_id, is_buy, limit_price, quantity, post_only=True
    ):
        data, order_id = self.create_order_rest_json(
            int(instrument_id), is_buy, limit_price, quantity, post_only
        )
        logger.info(data)
        req = self.client.post(
            f"{self.rest_url}/orders", json=data, headers=self.rest_headers
        )
        try:
            return req.json()
        except:
            return req.text()

    def rest_create_market_order(self, instrument_id, is_buy, quantity):
        limit_price = 0
        if is_buy:
            limit_price = 2**256 - 1

        data, order_id = self.create_order_rest_json(
            int(instrument_id),
            is_buy,
            limit_price,
            quantity,
            decimals=1,
            post_only=False,
        )

        req = self.client.post(
            f"{self.rest_url}/orders", json=data, headers=self.rest_headers
        )
        return req.json()

    def rest_cancel_order(self, order_id):
        req = self.client.delete(
            f"{self.rest_url}/orders/{order_id}", headers=self.rest_headers
        )
        logger.info(req.json())
        return req.json()

    def rest_get_account(self):
        req = self.client.get(f"{self.rest_url}/account", headers=self.rest_headers)
        return req.json()

    def rest_get_portfolio(self):
        req = self.client.get(f"{self.rest_url}/portfolio", headers=self.rest_headers)
        return req.json()

    def rest_get_open_orders(self):
        req = self.client.get(
            f"{self.rest_url}/orders", json={}, headers=self.rest_headers
        )
        return req.json()

    def rest_cancel_all_orders(
        self,
        instrument_type=None,
        asset=None,
    ):
        body = {}
        if instrument_type:
            body["instrument_type"] = instrument_type

        if asset:
            body["asset"] = asset

        req = self.client.delete(
            f"{self.rest_url}/orders-all", json=body, headers=self.rest_headers
        )
        return req.json()


    # Public WS Subscriptions
    async def subscribe_tickers(self, asset):
        await self.send(
            json.dumps(
                {
                    "op": "subscribe",
                    "data": [f"ticker:{asset}:OPTION"],
                }
            )
        )

    # async def subscribe_ticker(self, channel):
    #     msg = json.dumps(
    #         {
    #             "op": "subscribe",
    #             "data": [channel],
    #         }
    #     )
    #     await self.send(msg)

    async def subscribe_markprice(self, asset):
        await self.send(
            json.dumps(
                {
                    "op": "subscribe",
                    "data": [f"markprice:{asset}:OPTION"],
                }
            )
        )

    async def subscribe_orderbook(self, instrument_name):
        subscription_message = {
            "op": "subscribe",
            "data": [f"orderbook:{instrument_name}"]
        }
        await self.send(json.dumps(subscription_message))

        # 等待确认消息
        try:
            confirmation = await asyncio.wait_for(self.read_messages(), timeout=5)
            confirmation_data = json.loads(confirmation)

            if confirmation_data.get("op") == "subscribed" and f"orderbook:{instrument_name}" in confirmation_data.get("data", []):
                logger.info(f"Successfully subscribed to orderbook:{instrument_name}")
            else:
                logger.error(f"Failed to subscribe to orderbook:{instrument_name}: {confirmation_data}")
                # 可以在这里添加重试逻辑或抛出异常

        except asyncio.TimeoutError:
            logger.error(f"Timeout while waiting for subscription confirmation for orderbook:{instrument_name}")
            # 可以在这里添加重试逻辑或抛出异常

    async def subscribe_trades(self, instrument_name):
        await self.send(
            json.dumps(
                {
                    "op": "subscribe",
                    "data": [f"trades:{instrument_name}"],
                }
            )
        )

    async def subscribe_index(self, asset):
        await self.send(json.dumps({"op": "subscribe", "data": [f"index:{asset}"]}))

    # Private WS Subscriptions
    # async def subscribe_orders(self):
    #     subscription_message = json.dumps({
    #         "op": "subscribe",
    #         "data": ["orders"]
    #     })
    #     await self.send(subscription_message)
    #     # print("Subscribed to orders.")
    #


    # Private WS Commands
    def create_order_ws_json(
        self,
        instrument_id,
        is_buy,
        limit_price,
        quantity,
        post_only=True,
        mmp=True,
        price_decimals=10**6,
        amount_decimals=10**6,
    ):
        timestamp = int(time.time())
        salt, signature, order_id = self.sign_order(
            instrument_id=instrument_id,
            is_buy=is_buy,
            limit_price=limit_price,
            quantity=quantity,
            timestamp=timestamp,
            price_decimals=price_decimals,
        )

        payload = {
            "instrument": instrument_id,
            "maker": self.wallet_address,
            "is_buy": is_buy,
            "amount": str(int(round(quantity * amount_decimals, is_buy))),
            "limit_price": str(int(round(limit_price * price_decimals, is_buy))),
            "salt": str(salt),
            "signature": signature,
            "post_only": post_only,
            "mmp": mmp,
            "timestamp": timestamp,
        }
        return payload, order_id

    def create_order_rest_json(
        self,
        instrument_id,
        is_buy,
        limit_price,
        quantity,
        post_only=True,
        reduce_only=False,
        close_position=False,
        price_decimals=10**6,
        amount_decimals=10**6,
        trigger=None,
        stop=None,
    ):
        timestamp = int(time.time())
        salt, signature, order_id = self.sign_order(
            instrument_id=instrument_id,
            is_buy=is_buy,
            limit_price=limit_price,
            quantity=quantity,
            timestamp=timestamp,
            price_decimals=price_decimals,
        )
        payload = {
            "maker": self.wallet_address,
            "is_buy": is_buy,
            "instrument": instrument_id,
            "limit_price": str(int(round(limit_price * price_decimals, is_buy))),
            "amount": str(int(round(quantity * amount_decimals, is_buy))),
            "salt": str(salt),
            "signature": signature,
            "post_only": post_only,
            "reduce_only": reduce_only,
            "close_position": close_position,
            "timestamp": timestamp,
        }
        if trigger and stop:
            payload["trigger"] = trigger
            payload["stop"] = stop

        return payload, order_id

    async def create_order(
        self,
        instrument_id,
        is_buy,
        limit_price,
        quantity,
        post_only=True,
        id=None,
        mmp=True,
    ):
        data, order_id = self.create_order_ws_json(
            instrument_id=int(instrument_id),
            is_buy=is_buy,
            limit_price=limit_price,
            quantity=quantity,
            post_only=post_only,
            mmp=mmp,
        )
        payload = {"op": "create_order", "data": data}
        if id:
            payload["id"] = id

        logger.info(payload)
        await self.send(json.dumps(payload))

        return order_id

    async def edit_order(
        self,
        order_id,
        instrument_id,
        is_buy,
        limit_price,
        quantity,
        id=None,
        post_only=True,
        mmp=True,
    ):
        timestamp = int(time.time())
        instrument_id = int(instrument_id)
        salt, signature, new_order_id = self.sign_order(
            instrument_id=instrument_id,
            is_buy=is_buy,
            limit_price=limit_price,
            quantity=quantity,
            timestamp=timestamp,
        )
        payload = {
            "op": "edit_order",
            "data": {
                "order_id": order_id,
                "instrument": instrument_id,
                "maker": self.wallet_address,
                "is_buy": is_buy,
                "amount": str(int(round(quantity * 10**6, is_buy))),
                "limit_price": str(int(round(limit_price * 10**6, is_buy))),
                "salt": str(salt),
                "signature": signature,
                "post_only": post_only,
                "mmp": mmp,
                "timestamp": timestamp,
            },
        }

        if id:
            payload["id"] = id

        logger.info(payload)
        await self.send(json.dumps(payload))

        return new_order_id

    async def cancel_order(self, order_id):
        if not order_id:
            return

        payload = {"op": "cancel_order", "data": {"order_id": order_id}}
        logger.info(payload)
        await self.send(json.dumps(payload))

    async def cancel_all_orders(self):
        payload = {"op": "cancel_all_orders", "data": {}}
        await self.send(json.dumps(payload))

    def sign_order(
        self,
        instrument_id,
        is_buy,
        limit_price,
        quantity,
        timestamp,
        price_decimals=10**6,
        amount_decimals=10**6,
    ):
        salt = random.randint(0, 10**10)  # We just need a large enough number

        order_struct = Order(
            maker=self.wallet_address,  # The wallet"s main address
            isBuy=is_buy,
            limitPrice=int(round(limit_price * price_decimals, is_buy)),
            amount=int(round(quantity * amount_decimals, is_buy)),
            salt=salt,
            instrument=instrument_id,
            timestamp=timestamp,
        )
        logger.info(self.signing_domain)
        domain = make_domain(**self.signing_domain)
        signable_bytes = keccak(order_struct.signable_bytes(domain=domain))
        return (
            salt,
            Account._sign_hash(signable_bytes, self.signing_key).signature.hex(),
            f"0x{signable_bytes.hex()}",
        )

