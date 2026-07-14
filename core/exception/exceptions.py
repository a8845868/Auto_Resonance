import builtins

from loguru import logger


class Exception(Exception):

    def __init__(self, message):
        super().__init__()
        logger.error(message)


class TypeError(Exception):

    def __init__(self, message):
        super().__init__(message)


class StopExecution(Exception):

    def __init__(self):
        builtins.Exception.__init__(self, "停止执行程序")
