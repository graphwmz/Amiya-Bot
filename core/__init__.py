import os
import copy
import time
import jieba
import datetime
import traceback

from typing import List, Union
from amiyabot import (
    MultipleAccounts,
    PluginInstance,
    HttpServer,
    Message,
    Event,
    Chain,
    ChainBuilder,
    Equal,
    log
)
from amiyabot.adapters import BotAdapterProtocol
from amiyabot.adapters.tencent import TencentBotInstance
from amiyabot.network.httpRequests import http_requests
from amiyabot.builtin.lib.timedTask import tasks_control

from core.database.messages import MessageRecord
from core.database.bot import BotAccounts
from core.resource import remote_config
from core.resource.botResource import BotResource
from core.resource.arknightsGameData import ArknightsGameData, ArknightsConfig
from core.lib.gitAutomation import GitAutomation
from core.util import read_yaml, create_dir

serve_conf = read_yaml('config/server.yaml')

app = HttpServer(serve_conf.host, serve_conf.port, auth_key=serve_conf.authKey)
bot = MultipleAccounts(*BotAccounts.get_all_account())

bot.set_prefix_keywords(['阿米娅', '阿米兔', '兔兔', '兔子', '小兔子', 'Amiya', 'amiya'])
jieba.del_word('兔子')

gamedata_repo = GitAutomation('resource/gamedata', remote_config.remote.gamedata)

message_record = []


class LazyLoadPluginInstance(PluginInstance):
    def __init__(self,
                 name: str,
                 version: str,
                 plugin_id: str,
                 plugin_type: str = None,
                 description: str = None,
                 document: str = None):
        super().__init__(
            name,
            version,
            plugin_id,
            plugin_type,
            description,
            document
        )

    def load(self): ...


def load_resource():
    gamedata_repo.update()
    BotResource.download_bot_resource()
    ArknightsConfig.initialize()
    ArknightsGameData.initialize()


async def load_plugins():
    create_dir('plugins')
    count = 0
    for root, dirs, files in os.walk('plugins'):
        for file in files:
            if file.endswith('.zip'):
                log.info(f'installing plugin {file}')
                try:
                    res = bot.install_plugin(os.path.join(root, file), extract_plugin=True)
                    if res:
                        count += 1
                except Exception as e:
                    log.error(e, 'plugin install error:')
        break

    # 然后对所有插件执行懒加载（如果有的话）
    for plugin_id, item in bot.plugins.items():
        if isinstance(item, LazyLoadPluginInstance):
            log.info(f'lazy load plugins {plugin_id}')
            item.load()

    if count:
        log.info(f'successfully loaded {count} plugin(s).')


class SourceServer(ChainBuilder):
    @staticmethod
    async def image_getter_hook(image):
        if type(image) is bytes:
            res = await http_requests.upload(f'{remote_config.remote.resource}/upload', image)
            if res:
                return f'{remote_config.remote.resource}/images?path=' + res.strip('"')
        return image


def exec_before_init(coro):
    init_task.append(coro())
    return coro


async def send_to_console_channel(chain: Chain):
    main_bot: List[BotAccounts] = BotAccounts.select().where(BotAccounts.is_main == 1)
    for item in main_bot:
        if item.console_channel:
            await bot[item.appid].send_message(chain, channel_id=item.console_channel)


async def heartbeat():
    for item in bot:
        await http_requests.get(f'https://server.amiyabot.com:8020/heartbeat?appid={item.appid}')


@bot.on_exception()
async def _(err: Exception, instance: BotAdapterProtocol, data: Union[Message, Event]):
    chain = Chain()

    if type(instance) is TencentBotInstance:
        chain.builder = SourceServer()

    info = [
        'Adapter: ' + str(instance),
        'Bot: ' + str(instance.appid),
        'Channel: ' + str(data.channel_id),
        'User: ' + str(data.user_id),
        '\n' + data.text
    ]

    content = chain.text('\n'.join(info)).text_image(traceback.format_exc())

    await send_to_console_channel(content)


@bot.before_bot_reply
async def _(data: Message, _):
    message_record.append({
        'app_id': data.instance.appid,
        'user_id': data.user_id,
        'channel_id': data.channel_id,
        'msg_type': data.message_type or 'channel',
        'classify': 'call',
        'create_time': int(time.time())
    })


@tasks_control.timed_task(each=60)
async def _():
    await heartbeat()


@tasks_control.timed_task(each=60)
async def _():
    global message_record
    MessageRecord.batch_insert(copy.deepcopy(message_record))
    message_record = []


@tasks_control.timed_task(each=3600)
async def _():
    timestamp = int(
        time.mktime(
            time.strptime(
                (datetime.datetime.now() + datetime.timedelta(days=-7)).strftime('%Y%m%d'),
                '%Y%m%d'
            )
        )
    )
    MessageRecord.delete().where(MessageRecord.create_time < timestamp).execute()


init_task = [
    heartbeat(),
    tasks_control.run_tasks()
]
