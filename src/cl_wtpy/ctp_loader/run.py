import time
from wtpy import ContractLoader,LoaderType

# 通过 CTP 接口加载合约信息，config.ini 中需配置 CTP 账户及前置地址
loader = ContractLoader(lType = LoaderType.LT_CTP)
print('press ctrl-c to exit')
try:
    loader.start(cfgfile="config.ini")
    while True:
        time.sleep(1)
except KeyboardInterrupt as e:
    exit(0)