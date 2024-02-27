import asyncio
from aevo import AevoClient
from dotenv import load_dotenv
import os
import json  # 导入json模块
from dingding import *
import logging
import pickle

# 加载.env文件
load_dotenv()
error_webhook_url = 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key='

# 配置日志
logging.basicConfig(filename='grid_trading.log', level=logging.INFO, format='%(asctime)s %(message)s')
logger = logging.getLogger(__name__)

def calculate_opposite_price(price, price_step, is_buy):

    return round(price + price_step if is_buy else price - price_step, 2)


async def cancel_all_orders(aevo, instrument_id):

    cancel_response = await aevo.rest_cancel_all_orders(instrument_id)
    return cancel_response

async def create_order(aevo, instrument_id, is_buy, limit_price, quantity_per_grid, post_only):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, aevo.rest_create_order,
        instrument_id, is_buy, limit_price, quantity_per_grid, post_only
    )


async def initialize_grid(aevo, instrument_id, lower_price, upper_price, grid_size, quantity_per_grid,price_decimals):
    # 计算每个网格之间的价格步长
    price_step = (upper_price - lower_price) / grid_size
    grid_orders = []
    for i in range(grid_size + 1):
        # 计算网格价格
        grid_price = round(lower_price + i * price_step, price_decimals)
        # 创建买单
        buy_order_id = await create_order(aevo, instrument_id, True, grid_price, quantity_per_grid, True)
        grid_orders.append(buy_order_id)
        # 创建卖单
        sell_order_id = await create_order(aevo, instrument_id, False, grid_price, quantity_per_grid, True)
        grid_orders.append(sell_order_id)

        # 添加延迟以避免触发速率限制
        await asyncio.sleep(0.3)  # 延迟300毫秒

    return grid_orders

async def load_from_file_async(filename):
    # 确保文件存在
    if not os.path.isfile(filename):
        return None
    # 异步读取文件内容
    with open(filename, 'rb') as file:
        return pickle.load(file)


def send_notification(message):
    logging.error(message)
    send_wechat_work_msg(message, error_webhook_url)


async def handle_stop_loss(aevo, trade_asset, quantity_per_grid):
    await cancel_all_orders(aevo, trade_asset)
    await aevo.rest_create_market_order(instrument_id=trade_asset, is_buy=False,
                                        quantity=quantity_per_grid)
    msg = "Stop loss executed."
    logging.info(msg)
    send_notification(msg)


def get_value_safe(data, key, default=None):
    """
    安全地从字典中获取指定键的值。
    参数:
        data (dict): 需要获取值的字典。
        key (str): 要查找的键。
        default: 如果字典中不存在该键，将返回这个默认值。此参数是可选的，默认为None。
    返回:
        如果字典中存在该键，则返回对应的值；如果不存在，则返回default指定的默认值。
    """
    try:
        # 尝试从字典中获取键对应的值，如果键不存在则返回默认值
        return data.get(key, default)
    except (TypeError, AttributeError):
        # 如果data不是字典或者其他相关错误发生，则打印错误信息并返回默认值
        print(f"错误: 'data' 不是一个字典或者 'key' 不是一个有效的键。数据: {data}, 键: {key}")
        return default


def count_decimal_places(number):
    # 将浮点数转换成字符串
    number_str = str(number)
    # 如果小数点在字符串中，计算小数点后的位数
    if '.' in number_str:
        return len(number_str.split('.')[1])
    else:
        # 如果没有小数点，则小数位数为0
        return 0

async def main():
    try:
        # ==================== 网格交易配置 ====================
        trade_asset = 'ETH'  # 设置交易币种
        grid_size = 100  # 设置网格大小
        # 假设我们想要在当前价格等差范围内创建网格
        price_range_percent = 0.01  # 价格范围百分比
        grid_interval = 5  # 网格间隔（例如，每个网格间隔2美元，最小2刀）（会有误差，实际可能1.8刀左右一格）
        lower_price = 3180.0  # 网格下限价格
        upper_price = 3280.0  # 网格上限价格
        quantity_per_grid = 0.003  # 每个网格的交易数量
        stop_loss_price = 1900.0  # 止损价格
        price_decimals = 2  # ETH的价格小数点后两位
        grid_interval_or_quantity_per_grid = True  # 选择等差还是等分（默认等差）
        confidence_should_be_priced = True # 自适应价格，根据当前价格，价格范围百分比，网格大小，来设置网格
        # 是否平掉现有仓位从新开始
        initialization = True  #  (True是清理掉仓位，False保留现有仓位及订单，只进行 监控)
        # =====================================================

        aevo = AevoClient(
            signing_key='',  # 激活签名密钥 （aevo用户设置里获取）
            wallet_address='',  # 钱包地址
            api_key='',  # API key  （用户设置里获取）
            api_secret='',  # API secret
            env="mainnet",
        )
        if not aevo.signing_key:
            raise Exception("Signing key is not set. Please set the signing key in the AevoClient constructor.")
        # 计算固定价格间隔
        price_step = (upper_price - lower_price) / grid_size
        await aevo.open_connection()
        await asyncio.sleep(3)  # 等待连接稳定
        # 初始化清理已有的订单和仓位
        if initialization:
            logger.info('initialization=TRUE，初始化中，开始查询是否有未平仓位。')
            print("initialization=TRUE，初始化中，开始查询是否有未平仓位。")
            # 循环检查三次
            for attempt in range(3):
                logger.info(f'检查次数 {attempt + 1}')
                try:
                    account_info = aevo.rest_get_account()

                except Exception as e:
                    logger.error(f"获取账户信息失败: {e}")
                    continue
                positions = account_info['positions']

                if len(positions) > 0:  # 如果有未平仓位，那么 len(positions) 应该大于0
                    logger.info(f'监测到仓位: {positions}')
                    markets = aevo.get_markets(trade_asset)  # 获取行情数据
                    markets_data = [market for market in markets if
                               market['instrument_name'] == f'{trade_asset}-PERP'][0]  # 获取eth的数据
                    # instrument_id = get_value_safe(markets_data, 'instrument_id')  # 合约id
                    # instrument_name = get_value_safe(markets_data, 'instrument_name')  # 合约名称
                    amount_step = get_value_safe(markets_data, 'amount_step')  # 最小下单量
                    price_decimals = count_decimal_places(amount_step)  # 小数点后几位
                    # max_notional_value = get_value_safe(markets_data, 'max_notional_value')  # 最大下单量
                    mark_price = float(get_value_safe(markets_data, 'mark_price'))  # 最新成交价格
                    # is_active = get_value_safe(markets_data, 'is_active')  # TRue活跃中
                    # max_leverage = get_value_safe(markets_data, 'max_leverage')  # 最大杠杆
                    for position in positions:
                        if position['instrument_name'] == f'{trade_asset}-PERP':
                            try:
                                # 获取当前市场价格等数据
                                instrument_id, cpquantity, side = position['instrument_id'], float(position['amount']), \
                                position['side']
                                is_buy = True if side == 'sell' else False
                                if is_buy:
                                    # 空单平仓，创建市价买单，价格设置为略高于市场价格，例如市场价格的1.02倍
                                    limit_price = round(mark_price * 1.1, price_decimals)
                                else:
                                    # 多单平仓，创建市价卖单，直接将价格设置为0
                                    limit_price = round(mark_price * 0.9, price_decimals)

                                # 创建平仓订单
                                response = aevo.rest_create_order(
                                    instrument_id=instrument_id,
                                    is_buy=is_buy,
                                    limit_price=limit_price,
                                    quantity=cpquantity,
                                    post_only=False
                                )
                                logger.info(f'平仓订单响应: {response}')
                                await asyncio.sleep(2)

                            except Exception as e:
                                logger.error(f"创建订单失败: {e}")
                try:

                    cancel_response = aevo.rest_cancel_all_orders()
                    logger.info(f'取消所有挂单响应: {cancel_response}')
                    await asyncio.sleep(1)
                except Exception as e:
                    logger.error(f"取消订单失败: {e}")
                if attempt == 2 and positions:
                    logger.warning('尝试三次后仍有未平仓位，可能需要手动干预。')
                    break
                await asyncio.sleep(1)

            else:
                logger.info('没有检测到未平仓位。')
                pass
        else:
            logger.info('保留现有仓位及订单，只进行监控。')

        # 获取当前市场价格
        markets = aevo.get_markets(trade_asset)  # 获取行情数据
        markets_data = [market for market in markets if
                   market['instrument_name'] == f'{trade_asset}-PERP'][0]  # 获取eth的数据
        instrument_id = get_value_safe(markets_data, 'instrument_id')  # 合约id
        amount_step = float(get_value_safe(markets_data, 'amount_step'))  # 最小下单量
        price_decimals = count_decimal_places(amount_step)  # 小数点后几位
        # max_notional_value = get_value_safe(markets_data, 'max_notional_value')  # 最大下单量
        mark_price = float(get_value_safe(markets_data, 'mark_price')) # 最新成交价格
        # 计算网格范围
        if grid_interval_or_quantity_per_grid:  # 等差网格情况下进行以下操作
            # 计算网格的总覆盖范围
            total_range = int(grid_interval) * (grid_size - 1)

            # 计算网格的上下限
            lower_price = mark_price - (total_range / 2)
            upper_price = mark_price + (total_range / 2)
            logging.info(f"等差网格计算中--上限: {upper_price}, 下限: {lower_price}")
        else:  # 等分网格情况下进行以下操作
            # 根据当前市场价格调整网格范围
            price_range = mark_price * price_range_percent
            lower_price = mark_price - price_range
            upper_price = mark_price + price_range
            logging.info(f"等分网格计算中--上限: {upper_price}, 下限: {lower_price}")
        # 初始化网格
        # 根据当前市场价格调整网格范围
        if initialization:
            if confidence_should_be_priced:

                print(f"动态网格启动，根据当前市场价格 {mark_price} 调整网格范围：{lower_price} - {upper_price}")
                # 初始化网格

                grid_orders = await initialize_grid(aevo, instrument_id, lower_price, upper_price, grid_size,
                                                    quantity_per_grid,price_decimals)
                logging.info("初始化网格成功.")
                print("初始化网格成功.")

            elif lower_price <= mark_price <= upper_price:
                print(f'固定网格上下限设置价格，网格范围：{lower_price} - {upper_price}')
                # 初始化网格
                grid_orders = await initialize_grid(aevo, instrument_id, lower_price, upper_price, grid_size,
                                                    quantity_per_grid,price_decimals)
                logging.info("初始化网格成功.")
                print("初始化网格成功.")

            else:
                # 当前价格不在网格区间内，跳过下单
                print(f"当前价格 {mark_price} 超出固定网格范围 ({lower_price}, {upper_price})，不执行固定网格下单")

                logger.error("未能获取有效的成交信息。")
                if mark_price <= stop_loss_price:
                    await handle_stop_loss(aevo, trade_asset, quantity_per_grid)
                    # break  # 停止策略运行
                        # 记录止损操作日志

        print('初始化完成，开始监控订单和成交记录')
        await asyncio.sleep(5)  # 如果需要，等待初始化

        # 启动消息处理器作为一个单独的任务
        message_handler_task = asyncio.create_task(aevo.message_handler(grid_interval,price_decimals))
        # 同时启动心跳任务
        heartbeat_task = asyncio.create_task(aevo.start_heartbeat())
        # 启动其他协程（例如，订阅订单和行情）
        # await aevo.subscribe_ticker(f"{trade_asset}-PERP")   # 订阅行情
        await aevo.subscribe_fills() # 订阅订单
        # 同时运行两个任务
        await asyncio.gather(message_handler_task, heartbeat_task)

    except Exception as e:
        logger.error(f"发生错误: {e}")
        if message_handler_task:
            message_handler_task.cancel()  # 尝试取消message_handler任务
            try:
                await message_handler_task  # 等待message_handler任务完成
            except asyncio.CancelledError:
                logger.info("message_handler任务已被取消")

    finally:
        # 如果message_handler_task还没完成，尝试再次取消
        if message_handler_task and not message_handler_task.done():
            message_handler_task.cancel()
            try:
                await message_handler_task
            except asyncio.CancelledError:
                logger.info("message_handler任务已被取消")
        logger.info("程序结束")


if __name__ == "__main__":
    asyncio.run(main())
