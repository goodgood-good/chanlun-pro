"""chanlun.persistence —— 持久化层(DB 访问 + 文件缓存)。

db: MySQL 访问单例 DB + ORM 表映射(TableBy*);file_db: K线/缠论对象/TV chart
文件缓存(fdb/FileCacheDB)。原 chanlun.db / chanlun.file_db 路径保留为 facade
re-export 兼容 ~41 调用方(web 后端/exchange/trader/cl_utils)与旧 pickle。
见 memory project_chanlun_dir_structure_audit。
"""
