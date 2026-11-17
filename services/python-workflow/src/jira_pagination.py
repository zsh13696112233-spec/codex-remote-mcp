"""Jira 列表页码参数与越界收敛；旧游标接口仍保留。"""


def validate_page(page, limit, before=None):
    if (page is not None and (type(page) is not int or page < 1 or before is not None)
            or before is not None and (type(before) is not int or before < 1)
            or type(limit) is not int or not 1 <= limit <= 100):
        raise ValueError('分页参数无效。')


def page_window(total, page, limit):
    page = min(page, max(1, (total + limit - 1) // limit))
    return {'total': total, 'page': page, 'pageSize': limit}, (page - 1) * limit
