from ansible.inventory.host import Host
from ansible.playbook.task import Task

class TaskResult:
    _task: Task
    _host: Host
    _result: object
