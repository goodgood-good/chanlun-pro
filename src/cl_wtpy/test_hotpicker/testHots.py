from wtpy.apps import WtHotPicker, WtCacheMonExchg, WtCacheMonSS, WtMailNotifier
import datetime
import logging

logging.basicConfig(filename='hotsel.log', level=logging.INFO, filemode="w",
    format='[%(asctime)s - %(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S')

console = logging.StreamHandler()
console.setLevel(logging.INFO)
formatter = logging.Formatter(fmt="[%(asctime)s - %(levelname)s] %(message)s", datefmt='%m-%d %H:%M:%S')
console.setFormatter(formatter)
logging.getLogger('').addHandler(console)

def rebuild_hot_rules():
    '''
    全量重建主力合约切换规则（hots.json）和次主力规则（seconds.json）。
    首次使用或需要重算历史规则时调用。
    '''
    # 从交易所官网实时拉取行情快照作为换月判断依据
    cacher = WtCacheMonExchg()

    # 从datakit落地的行情快照直接读取（备选方案，离线环境使用）
    # cacher = WtCacheMonSS("../storage/his/snapshot/")

    picker = WtHotPicker(hotFile="hots.json", secFile="seconds.json")
    picker.set_cacher(cacher)

    sDate = datetime.datetime.strptime("2019-01-01", '%Y-%m-%d')
    eDate = datetime.datetime.strptime("2021-08-21", '%Y-%m-%d')  # None 则自动取当前日期
    hotRules,secRules = picker.execute_rebuild(sDate, eDate, wait=True)
    print(hotRules)
    print(secRules)

def daily_hot_rules():
    '''
    增量更新主力合约切换规则，每日收盘后调用以追加最新换月记录。
    '''
    cacher = WtCacheMonExchg()

    # 从datakit落地的行情快照直接读取（备选方案，离线环境使用）
    # cacher = WtCacheMonSS("../storage/his/snapshot/")

    picker = WtHotPicker(hotFile="hots.json", secFile="seconds.json")
    picker.set_cacher(cacher)

    # 邮件通知配置示例（按需取消注释并填写真实账户信息）
    # notifier = WtMailNotifier(user="yourmailaddr", pwd="yourmailpwd", host="smtp.exmail.qq.com", port=465, isSSL=True)
    # notifier.add_receiver(name="receiver1", addr="receiver1@qq.com")
    # picker.set_mail_notifier(notifier)

    eDate = datetime.datetime.strptime("2016-03-01", '%Y-%m-%d')  # None 则自动取当前日期
    picker.execute_increment(eDate)

rebuild_hot_rules()
input("press enter key to exit\n")