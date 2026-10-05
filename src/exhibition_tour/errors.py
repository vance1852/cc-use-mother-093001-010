"""巡展调度服务使用的可预期业务异常。"""


class TourError(Exception):
    """所有可预期业务异常的基类。"""

    code = "tour_error"
    status = 400


class ValidationError(TourError):
    """输入字段不符合业务约束。"""

    code = "validation_error"


class NotFoundError(TourError):
    """请求引用的业务对象不存在。"""

    code = "not_found"
    status = 404


class PermissionDenied(TourError):
    """参与方没有执行当前动作的权限。"""

    code = "permission_denied"
    status = 403


class ConflictError(TourError):
    """请求编号、业务唯一键或资源占用与既有内容冲突。"""

    code = "conflict"
    status = 409


class StateError(TourError):
    """对象当前状态不允许该动作。"""

    code = "invalid_state"
    status = 409
