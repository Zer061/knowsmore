import errno
import json
import os
import re
import shutil
import sqlite3
import time
import mimetypes
import tempfile
from enum import Enum
from pathlib import Path
from zipfile import ZipFile
from argparse import _ArgumentGroup, Namespace
from clint.textui import progress
from neo4j import GraphDatabase, exceptions

from knowsmore.cmdbase import CmdBase
from knowsmore.password import Password
from knowsmore.util.color import Color
from knowsmore.util.database import Database
from knowsmore.util.logger import Logger
from knowsmore.util.tools import Tools


class Bloodhound(CmdBase):
    class ImportMode(Enum):
        Undefined = 0
        Import = 1
        MarkOwned = 2

    filename = ''
    db = None
    chain_enabled = False
    domain_cache = {}
    mode = ImportMode.Undefined

    class BloodhoundFile:
        file_name = None
        type = 'unknown'
        items = 0
        version = 0
        order = 99999
        bh_connection = None

        def __init__(self, file_name: str):
            self.file_name = file_name

            try:
                json_data = self.get_json()

                meta = json_data.get('meta', {})
                self.type = meta.get('type', 'unknown').lower()
                self.items = meta.get('count', 0)
                self.version = meta.get('version', 0)

                if self.type == "domains":
                    self.order = 1
                elif self.type == "groups":
                    self.order = 2
                elif self.type == "computers":
                    self.order = 3
                elif self.type == "users":
                    self.order = 4

            except KeyboardInterrupt as e:
                raise e
            except:
                pass

        def get_json(self):
            with open(self.file_name, 'r', encoding="UTF-8", errors="surrogateescape") as f:
                return json.load(f)

    class BloodHoundConnection:

        database=None

        def __init__(self, uri, user, password, database):
            self.driver = GraphDatabase.driver(uri, auth=(user, password))
            try:
                self.driver.verify_connectivity()

            except exceptions.ServiceUnavailable as e:
                raise e
            except exceptions.AuthError as e:
                raise e
            except Exception as e:
                #print(e.__class__)
                raise e

            self.database = database

        def close(self):
            self.driver.close()

        def get_all_owned(self) -> list:
            with self.driver.session(database=self.database) as session:
                return session.execute_read(self._get_owned)

        def set_owned(self, account: str, owned: bool = True):
            with self.driver.session(database=self.database) as session:
                return session.execute_write(self._set_owned, account, owned)

        @staticmethod
        def _get_owned(tx):
            accounts = []
            result = tx.run("MATCH (n) WHERE n.owned IS NOT NULL RETURN n.name as name, n.owned as owned")
            for record in result:
                accounts.append(
                    {
                        'name': record.get("name", None),
                        'owned': record.get("owned", None),
                    }
                )
            return accounts

        @staticmethod
        def _set_owned(tx, account: str, owned: bool = True):
            result = tx.run("MATCH (n) WHERE (n.name = $account) SET n.owned = true RETURN n.name as name, n.owned as owned",
                            account=account,
                            owned=owned,
                            )
            rst = result.single()
            if rst is None:
                return None

            ret_data = {
                'name': rst.get("name", None),
                'owned': rst.get("owned", None),
            }
            return ret_data

    def __init__(self):
        super().__init__('bloodhound', 'Import BloodHound files')

    def add_flags(self, flags: _ArgumentGroup):
        flags.add_argument('--enable-group-chain',
                           action='store_true',
                           default=False,
                           dest=f'chain_enabled',
                           help=Color.s('Enable group chain calculation.'))

        flags.add_argument('-u',
                           action='store',
                           metavar='[neo4j username]',
                           type=str,
                           default='neo4j',
                           dest=f'neo4j_username',
                           help=Color.s('Neo4j Username to mark host/users as owned. (default: neo4j)'))

        flags.add_argument('-p',
                           action='store',
                           metavar='[neo4j password]',
                           type=str,
                           default='neo4j',
                           dest=f'neo4j_password',
                           help=Color.s('Neo4j Password to mark host/users as owned. (default: neo4j)'))

        flags.add_argument('-d',
                           action='store',
                           metavar='[neo4j database]',
                           type=str,
                           default='neo4j',
                           dest=f'neo4j_database',
                           help=Color.s('Neo4j Database to mark host/users as owned. (default: neo4j)'))

    def add_commands(self, cmds: _ArgumentGroup):
        cmds.add_argument('--import-data',
                          action='store',
                          metavar='[bloodhound file]',
                          type=str,
                          dest=f'bhfile',
                          help=Color.s('BloodHound file. Available parses: {G}.zip{W} and {G}.json{W}'))

        cmds.add_argument('--mark-owned',
                          action='store',
                          metavar='[neo4j host and port]',
                          type=str,
                          dest=f'neo4j_host',
                          help=Color.s('BloodHound Neo4j Database. host:port'))

    def load_from_arguments(self, args: Namespace) -> bool:
        if args.neo4j_host is not None and args.neo4j_host != '':

            host = args.neo4j_host
            port = 7687
            if ':' in host:
                (host, port) = host.split(':', 2)

            try:
                self.bh_connection = Bloodhound.BloodHoundConnection(
                    f'bolt://{host}:{port}',
                    args.neo4j_username,
                    args.neo4j_password,
                    args.neo4j_database,
                )

            except Exception as e:
                Logger.pl('{!} {R}error: Fail to connect with Neo4j Database: {O}%s{R} {W}\r\n' % (
                    e))
                Tools.exit_gracefully(1)

            self.mode = Bloodhound.ImportMode.MarkOwned

        elif args.bhfile is not None and args.bhfile.strip() != '':

            if not os.path.isfile(args.bhfile):
                Logger.pl('{!} {R}error: BloodHound filename is invalid {O}%s{R} {W}\r\n' % (
                    args.bhfile))
                Tools.exit_gracefully(1)

            try:
                with open(args.bhfile, 'r') as f:
                    # file opened for writing. write to it here
                    pass
            except IOError as x:
                if x.errno == errno.EACCES:
                    Logger.pl('{!} {R}error: could not open BloodHound file {O}permission denied{R}{W}\r\n')
                    Tools.exit_gracefully(1)
                elif x.errno == errno.EISDIR:
                    Logger.pl('{!} {R}error: could not open BloodHound file {O}it is an directory{R}{W}\r\n')
                    Tools.exit_gracefully(1)
                else:
                    Logger.pl('{!} {R}error: could not open BloodHound file {W}\r\n')
                    Tools.exit_gracefully(1)

            self.filename = args.bhfile
            self.chain_enabled = args.chain_enabled
            self.mode = Bloodhound.ImportMode.Import

        if self.mode == Bloodhound.ImportMode.Undefined:
            Logger.pl('{!} {R}error: Nor {O}--import-data{R} or {O}--mark-owned{R} was provided{W}\r\n')
            Tools.exit_gracefully(1)

        self.db = self.open_db(args)

        return True

    def run(self):

        if self.mode == Bloodhound.ImportMode.MarkOwned:

            db_cracked = self.db.select_raw(
                sql='select c.name, d.name as domain_name, c.type from credentials as c '
                'inner join passwords as p '
                'on c.password_id  = p.password_id  '
                'inner join domains as d '
                'on c.domain_id = d.domain_id  '
                'where p.length > 0',
                args=[]
            )

            with progress.Bar(label=" Marking as owned ", expected_size=len(db_cracked)) as bar:
                try:
                    for idx, row in enumerate(db_cracked):
                        bar.show(idx)

                        bh_name = f'{row["name"]}@{row["domain_name"]}'
                        if row['type'] == "M":
                            bh_name = f'{row["name"]}.{row["domain_name"]}'

                        self.bh_connection.set_owned(bh_name, True)

                    #print(self.bh_connection.get_all_owned())

                except KeyboardInterrupt as e:
                    raise e
                finally:
                    bar.hide = True
                    Tools.clear_line()


        elif self.mode == Bloodhound.ImportMode.Import:
            try:
                # Check file type
                mime = mimetypes.MimeTypes().guess_type(self.filename)[0]
                if mime == "application/zip":
                    # extract files
                    Color.pl('{?} {W}{D}BloodHound ZIP File identified, extracting...{W}')

                    with self.get_temp_directory() as tmpdirname:
                        try:
                            with ZipFile(self.filename, 'r') as zObject:
                                zObject.extractall(tmpdirname)

                            Color.pl('{?} {W}{D}checking file consistency...{W}')
                            t_files = [
                                f for f in self.get_files(tmpdirname)
                                if f is not None or f.strip() != ''
                            ]
                            files = []
                            with progress.Bar(label=" Parsing ", expected_size=len(t_files)) as bar:
                                try:
                                    for idx, f in enumerate(t_files):
                                        bar.show(idx)
                                        f1 = Bloodhound.BloodhoundFile(f)
                                        if f1.type != 'unknown':
                                            files.append(f1)
                                        else:
                                            Color.pl('{?} {W}{D}invalid file: {G}%s{W}' % f)

                                except KeyboardInterrupt as e:
                                    raise e
                                finally:
                                    bar.hide = True
                                    Tools.clear_line()

                            Color.pl('{?} {W}{O}%s{G} valid files in ZIP{W}' % len(files))

                            self.parse_files(files)

                        finally:
                            shutil.rmtree(tmpdirname)
                else:
                    f = Bloodhound.BloodhoundFile(self.filename)
                    if f.type == 'unknown':
                        Logger.pl('{!} {R}error: BloodHound file is invalid {O}%s{R} {W}\r\n' % (
                            f.file_name))
                        Tools.exit_gracefully(1)
                    self.parse_file([f])

            except KeyboardInterrupt as e:
                Tools.clear_line()
                print((" " * 180), end='\r', flush=True)
                Logger.pl("{!} {C}Interrupted by user{W}")
                raise e

    def parse_files(self, files: list[BloodhoundFile]):

        unsupported = [
            f for f in files
            if f.version != 4 and f.version != 5
        ]
        if len(unsupported) > 0:
            Logger.pl('{!} {R}error: Unsupported BloodHound Version:{W}')
            for f in unsupported:
                Color.pl('{!} {W}{D}%s: {G}v%d{W}' % (f.file_name, f.version))
            Tools.exit_gracefully(1)

        # Domains
        self.parse_domains_files(sorted([
            f for f in files
            if f.type == 'domains'
        ], key=lambda x: (x.order, x.file_name), reverse=False))

        # Groups
        self.parse_groups_file(sorted([
            f for f in files
            if f.type == 'groups'
        ], key=lambda x: (x.order, x.file_name), reverse=False))

        # Computers
        self.parse_computers_files(sorted([
            f for f in files
            if f.type == 'computers'
        ], key=lambda x: (x.order, x.file_name), reverse=False))

        # Users
        self.parse_users_file(sorted([
            f for f in files
            if f.type == 'users'
        ], key=lambda x: (x.order, x.file_name), reverse=False))

    def parse_computers_files(self, files: list[BloodhoundFile]):

        Color.pl('{?} {W}{D}importing computers...{W}')

        total = sum(f.items for f in files)
        with progress.Bar(label=" Processing ", expected_size=total) as bar:
            try:
                count = 0
                for file in files:
                    data = file.get_json().get('data', [])
                    for idx, dd in enumerate(data):
                        count += 1
                        bar.show(count)

                        oid = dd.get('ObjectIdentifier', None)
                        properties = dd.get('Properties', None)

                        if oid is None or properties is None:
                            raise Exception('Unable to parse domain data')

                        name = properties.get('name', None)
                        domain = properties.get('domain', None)
                        dn = properties.get('distinguishedname', None)

                        if name is None or domain is None or dn is None:
                            raise Exception('Unable to parse domain data')

                        name = name.lower()
                        domain = domain.lower()

                        if name.endswith(f'.{domain}'):
                            name = name.replace(f'.{domain}', '')

                        domain_id = self.get_domain(properties)

                        self.db.insert_or_update_credential(
                            domain=domain_id,
                            username=name,
                            groups='',
                            object_identifier=oid,
                            dn=dn,
                            ntlm_hash='',
                            type='M')

            except KeyboardInterrupt as e:
                raise e
            finally:
                bar.hide = True
                Tools.clear_line()

    def parse_domains_files(self, files: list[BloodhoundFile]):

        Color.pl('{?} {W}{D}importing domains...{W}')

        total = sum(f.items for f in files)
        with progress.Bar(label=" Processing ", expected_size=total) as bar:
            try:
                count = 0
                for file in files:
                    data = file.get_json().get('data', [])
                    for idx, dd in enumerate(data):
                        count += 1
                        bar.show(count)

                        oid = dd.get('ObjectIdentifier', None)
                        properties = dd.get('Properties', None)

                        if oid is None or properties is None:
                            raise Exception('Unable to parse domain data')

                        name = properties.get('name', None)
                        domain = properties.get('domain', None)
                        dn = properties.get('distinguishedname', None)

                        if name is None or domain is None or dn is None:
                            raise Exception('Unable to parse domain data')

                        self.db.insert_or_get_domain(
                            domain=domain,
                            dn=dn,
                            object_identifier=oid)
            except KeyboardInterrupt as e:
                raise e
            finally:
                bar.hide = True
                Tools.clear_line()

    def parse_groups_file(self, files: list[BloodhoundFile]):
        groups = {}

        Color.pl('{?} {W}{D}loading groups...{W}')

        total = sum(f.items for f in files)
        with progress.Bar(label=" Processing ", expected_size=total) as bar:
            try:
                count = 0
                for file in files:
                    data = file.get_json().get('data', [])
                    for idx, dd in enumerate(data):
                        count += 1
                        bar.show(count)

                        gid = dd.get('ObjectIdentifier', None)
                        properties = dd.get('Properties', None)

                        if gid is None or properties is None:
                            raise Exception('Unable to parse domain data')

                        name = properties.get('name', '@').split('@')[0]
                        dn = properties.get('distinguishedname', None)

                        domain_id = self.get_domain(properties)

                        groups[gid] = {
                            "name": name,
                            "domain_id": domain_id,
                            "object_identifier": gid,
                            "dn": dn,
                            "json_members": dd.get('Members', []),
                            "members": [],
                            "membership": []
                        }

                        # Step 1
                        members = dd.get('Members', [])
                        for g in members:
                            t = g.get('ObjectType', None)
                            oid = g['ObjectIdentifier']
                            if t == "Group":
                                groups[gid]['members'].append(oid)

            except KeyboardInterrupt as e:
                raise e
            finally:
                bar.hide = True
                Tools.clear_line()

        if len(groups) > 0:

            Color.pl('{?} {W}{D}calculating group chain...{W}' + ' ' * 50)
            cnt = len(groups)
            with progress.Bar(label=" Processing ", expected_size=cnt) as bar:
                try:
                    for idx, g in enumerate(groups):
                        bar.show(idx)

                        groups[g]['membership'] = [g1 for g1 in self.get_group_chain(groups, g, [])]

                except KeyboardInterrupt as e:
                    raise e
                finally:
                    bar.hide = True
                    Tools.clear_line()

            Color.pl('{?} {W}{D}inserting groups...{W}' + ' ' * 50)
            with progress.Bar(label=" Inserting ", expected_size=cnt) as bar:
                try:
                    for idx, g in enumerate(groups):
                        bar.show(idx)

                        self.db.insert_group(
                            domain=groups[g]['domain_id'],
                            object_identifier=groups[g].get('object_identifier', '') if groups[g].get(
                                'object_identifier', None) is not None else '',
                            name=groups[g]['name'],
                            dn=groups[g].get('dn', '') if groups[g].get('dn', None) is not None else '',
                            members=json.dumps(groups[g]['json_members']),
                            membership=','.join(groups[g]['membership'])
                        )

                except KeyboardInterrupt as e:
                    raise e
                finally:
                    bar.hide = True
                    Tools.clear_line()

    def parse_users_file(self, files: list[BloodhoundFile]):
        Color.pl('{?} {W}{D}loading groups from db...{W}' + ' ' * 50)
        user_groups = {}
        groups = {}

        db_groups = self.db.select('groups')

        with progress.Bar(label=" Loading ", expected_size=len(db_groups)) as bar:
            try:
                for idx, row in enumerate(db_groups):
                    bar.show(idx)

                    gid = row['group_id']
                    members = json.loads(row['members'])
                    for g in members:
                        t = g['ObjectType']
                        oid = g['ObjectIdentifier']
                        if t == "User":
                            ug = user_groups.get(oid, [])
                            ug.append(gid)
                            user_groups[oid] = ug

                    groups[gid] = {
                        'name': row['name'],
                        'membership': row['membership'].split(',')
                    }

            except KeyboardInterrupt as e:
                raise e
            finally:
                bar.hide = True
                Tools.clear_line()

        Tools.clear_line()
        Color.pl('{?} {W}{D}importing users...{W}' + ' ' * 50)
        total = sum(f.items for f in files)
        with progress.Bar(label=" Processing ", expected_size=total) as bar:
            try:
                count = 0
                for file in files:
                    data = file.get_json().get('data', [])
                    for idx, dd in enumerate(data):
                        count += 1
                        bar.show(count)

                        oid = dd.get('ObjectIdentifier', None)
                        properties = dd.get('Properties', None)

                        if oid is None or properties is None:
                            raise Exception('Unable to parse user data 1: %s' % json.dumps(dd))

                        name = properties.get('name', '@').split('@')[0].lower()
                        dn = properties.get('distinguishedname', None)

                        if name is None:
                            raise Exception('Unable to parse user data 2: %s' % json.dumps(dd))

                        domain_id = self.get_domain(properties)

                        # Hard-coded empty password
                        self.db.insert_or_update_credential(
                            domain=domain_id,
                            username=name,
                            groups=self.get_user_groups(groups, user_groups, oid),
                            object_identifier=oid,
                            dn=dn,
                            ntlm_hash='',
                            type='U')

            except KeyboardInterrupt as e:
                raise e
            finally:
                bar.hide = True
                Tools.clear_line()

    def get_user_groups(self, groups, user_groups, user_id):
        ug = user_groups.get(user_id, [])
        gids = []
        for g in ug:
            tmp = groups[g]['membership']
            for t in tmp:
                if t not in gids:
                    gids.append(t)

        group_names = []
        for g in gids:
            gname = groups.get(g, {}).get("name", None)
            if gname is not None:
                group_names.append(gname)

        return ', '.join(group_names)

    def get_files(self, path):
        for file in os.listdir(path):
            p1 = os.path.join(path, file)
            if os.path.isfile(p1):
                yield p1
            elif os.path.isdir(p1):
                yield from self.get_files(p1)

    def get_group_chain(self, groups, group_id, chain):

        if group_id in chain:
            return []

        grp = []
        grp.append(group_id)

        if not self.chain_enabled:
            return grp

        for g in groups:
            members = groups[g].get('members', [])
            if group_id in members:
                if g not in grp:
                    grp.append(g)
                    tmp = self.get_group_chain(groups, g, chain + grp)
                    for t in tmp:
                        if t not in grp:
                            grp.append(t)
        return grp

    def get_domain(self, properties):
        domain_name = properties.get('domain', None)
        if domain_name is None:
            properties.get('name', '@').split('@')[1].lower()
        domain_name = domain_name.lower()
        domain_sid = properties.get('domainsid', '')

        if domain_sid in self.domain_cache:
            return self.domain_cache[domain_sid]

        domain_id = self.db.insert_or_get_domain(
            domain=domain_name,
            object_identifier=domain_sid
        )

        if domain_id == -1:
            raise Exception('Unable to get/create domain from JSON: %s' % json.dumps(properties))

        self.domain_cache[domain_sid] = domain_id

        return domain_id
