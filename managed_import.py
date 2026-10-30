"""Read candidate bot characters from the currently selected database."""
import re
import pymysql


def validate_prefix(prefix):
    if not isinstance(prefix, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,23}', prefix):
        raise ValueError('账号前缀须以字母开头，最多24位，仅含字母、数字、下划线或短横线')
    return prefix


def query_candidates(config, prefix):
    prefix = validate_prefix(prefix)
    pattern = '^' + re.escape(prefix) + '[0-9]+(@game[.]sohu[.]com)?$'
    connection = pymysql.connect(host=config['db_host'], port=int(config['db_port']),
        user=config['db_user'], password=config['db_password'], charset='latin1',
        cursorclass=pymysql.cursors.DictCursor, connect_timeout=8, read_timeout=15)
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT accname, charguid, HEX(charname) AS name_hex, level, menpai '
                'FROM tlbbdb_main.t_char WHERE CONVERT(accname USING latin1) COLLATE latin1_bin REGEXP %s ORDER BY accname, charguid LIMIT 2001', (pattern,))
            rows = cursor.fetchall()
    finally:
        connection.close()
    if len(rows) > 2000:
        raise ValueError('匹配角色超过2000个，请使用更具体的账号前缀')
    candidates = []
    for row in rows:
        account = row['accname']
        if isinstance(account, bytes):
            account = account.decode('ascii')
        if not isinstance(account, str) or not re.fullmatch(pattern, account):
            raise ValueError('数据库返回的账号与前缀不一致，未导入')
        try:
            name = bytes.fromhex(row['name_hex']).decode('gb18030')
        except (ValueError, UnicodeDecodeError, TypeError):
            name = '角色名无法解码'
        candidates.append(dict(account=account, guid=int(row['charguid']),
                               role=name, level=int(row['level']), school=int(row['menpai'])))
    return candidates
