"""Legacy entry point deliberately cannot bypass the reviewed execution protocol."""
def run():
    raise RuntimeError('Usa Coordinator y Docker Proxy /executions; el runner local ya no ejecuta código dinámico')


if __name__ == '__main__':
    run()
