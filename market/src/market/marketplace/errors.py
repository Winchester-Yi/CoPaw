# -*- coding: utf-8 -*-
"""应用市场查询和元数据同步异常."""


class MarketplaceDatabaseUnavailableError(RuntimeError):
    """应用市场依赖的 TDSQL 当前不可用."""


class MarketplaceMetadataSyncError(RuntimeError):
    """NAS 内容已写入，但 TDSQL 元数据同步失败."""
