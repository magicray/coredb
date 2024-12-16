import sys
import json
import time
import boto3
import base64
import pickle
import sqlite3
import logging
import argparse
from logging import critical as log


class S3Bucket:
    def __init__(self, s3bucket, key_id, secret_key):
        tmp = s3bucket.split('/')
        self.bucket = tmp[-1]
        self.endpoint = '/'.join(tmp[:-1])

        self.s3 = boto3.client('s3', endpoint_url=self.endpoint,
                               aws_access_key_id=key_id,
                               aws_secret_access_key=secret_key)

    def get(self, key):
        ts = time.time()
        key = 'Cloud Synced SQLite/' + key
        obj = self.s3.get_object(Bucket=self.bucket, Key=key)
        octets = obj['Body'].read()
        assert (len(octets) == obj['ContentLength'])
        log('s3(%s) get(%s/%s) length(%d) msec(%d)',
            self.endpoint, self.bucket, key, len(octets),
            (time.time()-ts) * 1000)
        return octets

    def put(self, key, value, content_type='application/octet-stream'):
        ts = time.time()
        key = 'Cloud Synced SQLite/' + key
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=value,
                           ContentType=content_type)
        log('s3(%s) put(%s/%s) length(%d) msec(%d)',
            self.endpoint, self.bucket, key, len(value),
            (time.time()-ts) * 1000)


PYTYPES = dict(i=(int,), f=(int, float), t=(str,), b=(str, bytes))
SQLTYPES = dict(i='int', f='float', t='text', b='blob')


def validate_types(values):
    params = dict()

    for k, v in values.items():
        if v is not None:
            if type(v) not in PYTYPES[k[0]]:
                raise Exception('Invalid type for {}'.format(k))

        if 'b' == k[0] and type(v) is str:
            params[k] = base64.b64decode(v)
        else:
            params[k] = v

    return params


class Database:
    def __init__(self, db, s3):
        self.s3 = s3
        self.log = json.loads(self.s3.get('log.json'))
        self.txns = list()

        self.conn = sqlite3.connect(db)
        self.conn.execute('pragma journal_mode=wal')
        self.conn.execute('pragma synchronous=normal')
        self.conn.execute('''create table if not exists _kv(
                                 key   text primary key,
                                 value text)''')
        self.conn.execute("""insert or ignore into _kv(key, value)
                             values('lsn', 0)""")

        row = self.conn.execute("select value from _kv where key='lsn'")
        lsn = int(row.fetchone()[0])

        for i in range(lsn+1, self.log['total']+1):
            cur = self.conn.cursor()
            txn = pickle.loads(self.s3.get('logs/' + str(i)))

            for sql, params in txn:
                cur.execute(sql, params)
                log('applied(%d) %s', i, sql)

            self.conn.execute("update _kv set value=? where key='lsn'", [i])
            self.conn.commit()

    def __del__(self):
        if self.conn:
            self.conn.rollback()
            self.conn.close()

    def commit(self):
        self.log['total'] += 1

        self.s3.put('logs/' + str(self.log['total']), pickle.dumps(self.txns))
        self.s3.put('log.json', json.dumps(self.log), 'application/json')

        self.conn.execute("update _kv set value=? where key='lsn'",
                          [self.log['total']])
        self.conn.commit()

        self.txns = list()

    def execute(self, sql, params=dict()):
        try:
            cur = self.conn.cursor()
            self.txns.append((sql, params))
            cur.execute(sql, params)
            log('modified(%d) %s', cur.rowcount, sql)
            cur.close()
        except Exception as e:
            self.txns = None
            self.conn.rollback()
            self.conn.close()
            self.conn = None
            log('exception(%s) %s', e, sql)
            raise

    def create(self, table, primary_key):
        pk = list()
        for k in primary_key:
            pk.append('{} {} not null'.format(k, SQLTYPES[k[0]]))

        pk = ', '.join(pk)
        pk_constraint = ', '.join(primary_key)

        self.execute('create table {} ({}, primary key({}))'.format(
            table, pk, pk_constraint))

    def add(self, table, column):
        self.execute('alter table {} add column {} {}'.format(
            table, column, SQLTYPES[column[0]]))

    def rename(self, table, src_col, dst_col):
        if src_col[0] != dst_col[0]:
            raise Exception('DST column type should be same as SRC')

        self.execute('alter table {} rename column {} to {}'.format(
            table, src_col, dst_col))

    def drop(self, table, column):
        self.execute('alter table {} drop column {}'.format(table, column))

    def insert(self, table, values):
        cols = values.keys()
        params = validate_types(values)

        first = ','.join(cols)
        second = ','.join([':{}'.format(c) for c in cols])

        self.execute('insert into {}({}) values({})'.format(
            table, first, second), params)

    def update(self, table, set_dict, where_dict):
        set_dict = validate_types(set_dict)
        where_dict = validate_types(where_dict)

        params = dict()
        params.update({'set_'+k: v for k, v in set_dict.items()})
        params.update({'where_'+k: v for k, v in where_dict.items()})

        first = ', '.join('{}=:set_{}'.format(k, k) for k in set_dict)
        second = ' and '.join('{}=:where_{}'.format(k, k) for k in where_dict)

        self.execute('update {} set {} where {}'.format(
            table, first, second), params)

    def delete(self, table, where):
        params = validate_types(where)
        where = ' and '.join('{}=:{}'.format(k, k) for k in params)

        self.execute('delete from {} where {}'.format(table, where), params)


def main():
    logging.basicConfig(format='%(asctime)s %(process)d : %(message)s')

    args = argparse.ArgumentParser()

    args.add_argument(
        '--config', default='config.json',
        help='Object bucket configuration')

    args.add_argument('--db', help='Database Name')
    args.add_argument('--table', help='Table Name')
    args.add_argument('operation', help='Operation to be done')

    args.add_argument('--src', help='Old column name')
    args.add_argument('--dst', help='New column name')
    args.add_argument('--column', help='Column name')
    args.add_argument('--primary_key', help='Comma separated column list')

    args = args.parse_args()

    with open(args.config) as fd:
        conf = json.load(fd)
        s3 = S3Bucket(conf['s3bucket'], conf['s3bucket_auth_key'],
                      conf['s3bucket_auth_secret'])

    db = Database(args.db, s3)

    if 'create' == args.operation:
        db.create(args.table, args.primary_key.split(','))

    elif 'add' == args.operation:
        db.add(args.table, args.column)

    elif 'rename' == args.operation:
        db.rename(args.table, args.src, args.dst)

    elif 'drop' == args.operation:
        db.drop(args.table, args.column)

    elif 'insert' == args.operation:
        db.insert(args.table, json.loads(sys.stdin.read()))

    elif 'update' == args.operation:
        obj = json.loads(sys.stdin.read())
        db.update(args.table, obj, obj.pop('where'))

    elif 'delete' == args.operation:
        db.delete(args.table, json.loads(sys.stdin.read()))

    else:
        raise Exception('Invalid Operation : {}'.format(args.operation))

    db.commit()


if __name__ == '__main__':
    main()