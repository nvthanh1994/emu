import glob
import os
import uuid
import yaml
from pathlib import Path
import pkg_resources
import shutil
from subprocess import DEVNULL, PIPE, STDOUT, check_call, Popen, CalledProcessError
import sys

from app.utility.base_service import BaseService


class EmuService(BaseService):
    def __init__(self):
        self.log = self.add_service('emu_svc', self)
        self.emu_dir = os.path.join('plugins', 'emu')
        self.repo_dir = os.path.join(self.emu_dir, 'data/adversary-emulation-plans')
        self.data_dir = os.path.join(self.emu_dir, 'data')
        self.payloads_dir = os.path.join(self.emu_dir, 'payloads')

        self.log.debug('Checking for pyminizip dependency for decrypting adversary_emulation_library binaries...')
        self.log.debug('pyminizip installed version is %s' % pkg_resources.get_distribution('pyminizip').version)

    async def clone_repo(self, repo_url=None):
        """
        Clone the Adversary Emulation Library repository. You can use a specific url via
        the `repo_url` parameter (eg. if you want to use a fork).
        """
        if not repo_url:
            repo_url = 'https://github.com/center-for-threat-informed-defense/adversary_emulation_library'

        if not os.path.exists(self.repo_dir) or not os.listdir(self.repo_dir):
            self.log.debug('cloning repo %s' % repo_url)
            check_call(['git', 'clone', '--depth', '1', repo_url, self.repo_dir], stdout=DEVNULL, stderr=STDOUT)
            self.log.debug('clone complete')

    async def populate_data_directory(self, path_yaml=None):
        """
        Populate the 'data' directory with the Adversary Emulation Library abilities.
        """
        total, ingested, errors = 0, 0, 0
        if not path_yaml:
            path_yaml = os.path.join(self.repo_dir, '*', '**', '*.yaml')

        for filename in glob.iglob(path_yaml, recursive=True):
            plan_total, plan_ingested, plan_errors = await self._ingest_emulation_plan(filename)
            total += plan_total
            ingested += plan_ingested
            errors += plan_errors

        errors_output = f' and ran into {errors} errors' if errors else ''
        self.log.debug(f'Ingested {ingested} abilities (out of {total}) from emu plugin{errors_output}')

    async def decrypt_payloads(self):
        path_crypt_script = os.path.join(self.repo_dir, '*', 'Resources', 'utilities', 'crypt_executables.py')
        for crypt_script in glob.iglob(path_crypt_script):
            plan_path = crypt_script[:crypt_script.rindex('Resources') + len('Resources')]
            self.log.debug('attempting to decrypt plan payloads using %s with the password "malware"' % crypt_script)
            process = Popen([sys.executable, crypt_script, '-i', plan_path, '-p', 'malware', '--decrypt'], stdout=PIPE)
            with process.stdout:
                for line in iter(process.stdout.readline, b''):
                    if b'[-]' in line:
                        self.log.error(line.decode('UTF-8').rstrip())
                    else:
                        self.log.debug(line.decode('UTF-8').rstrip())
            exit_code = process.wait()
            if exit_code != 0:
                raise CalledProcessError(
                    returncode=exit_code,
                    cmd=process.args,
                    stderr=process.stderr
                )

    @staticmethod
    def get_adversary_from_filename(filename):
        base = os.path.basename(filename)
        return os.path.splitext(base)[0]

    """ PRIVATE """

    async def _ingest_emulation_plan(self, filename):
        at_total, at_ingested, errors = 0, 0, 0
        emulation_plan = self.strip_yml(filename)[0]

        abilities = []
        details = dict()
        adversary_facts = []
        for entry in emulation_plan:
            if 'emulation_plan_details' in entry:
                details = entry['emulation_plan_details']
                if not self._is_valid_format_version(entry['emulation_plan_details']):
                    return 0, 0, 1

        if 'adversary_name' not in details:
            return 0, 0, 1

        for entry in emulation_plan:
            if await self._is_ability(entry):
                at_total += 1
                try:
                    ability_id, ability_facts = await self._save_ability(entry)
                    adversary_facts.extend(ability_facts)
                    abilities.append(ability_id)
                    at_ingested += 1
                except Exception as e:
                    self.log.error(e)
                    errors += 1

        await self._save_adversary(id=details.get('id', str(uuid.uuid4())),
                                   name=details.get('adversary_name', filename),
                                   description=details.get('adversary_description', filename),
                                   abilities=abilities)

        await self._save_source(details.get('adversary_name', filename), adversary_facts)
        return at_total, at_ingested, errors

    @staticmethod
    def _is_valid_format_version(details):
        try:
            return float(details['format_version']) >= 1.0
        except:
            return False

    async def _write_adversary(self, data):
        d = os.path.join(self.data_dir, 'adversaries')

        if not os.path.exists(d):
            os.makedirs(d)

        file_path = os.path.join(d, '%s.yml' % data['id'])
        with open(file_path, 'w') as f:
            f.write(yaml.dump(data))

    async def _save_adversary(self, id, name, description, abilities):
        adversary = dict(
            id=id,
            name=name,
            description='%s (Emu)' % description,
            atomic_ordering=abilities
        )
        await self._write_adversary(adversary)

    @staticmethod
    async def _is_ability(data):
        if {'id', 'platforms'}.issubset(set(data.keys())):
            return True
        return False

    async def _write_ability(self, data):
        d = os.path.join(self.data_dir, 'abilities', data['tactic'])
        if not os.path.exists(d):
            os.makedirs(d)
        file_path = os.path.join(d, '%s.yml' % data['id'])
        with open(file_path, 'w') as f:
            f.write(yaml.dump([data]))

    @staticmethod
    def get_privilege(executors):
        try:
            for ex in executors:
                if 'elevation_required' in ex:
                    return 'Elevated'
                return False
        except:
            return False

    async def _save_ability(self, ab):
        """
        Return True iif an ability was saved.
        """

        ability = dict(
            id=ab.pop('id', str(uuid.uuid4())),
            name=ab.pop('name', ''),
            description=ab.pop('description', ''),
            tactic='-'.join(ab.pop('tactic', '').lower().split(' ')),
            technique=dict(name=ab.get('technique', dict()).get('name'),
                           attack_id=ab.pop('technique', dict()).get('attack_id')),
            repeatable=ab.pop('repeatable', False),
            requirements=ab.pop('requirements', []),
            platforms=ab.pop('platforms')
        )

        privilege = self.get_privilege(ab.get('executors'))
        if privilege:
            ability['privilege'] = privilege

        payloads = []
        facts = []

        for platform in ability.get('platforms', dict()).values():
            for executor_details in platform.values():
                if executor_details.get('payloads'):
                    payloads.extend(executor_details['payloads'])

        for fact, details in ab.get('input_arguments', dict()).items():
            if details.get('default'):
                facts.append(dict(trait=fact, value=details.get('default')))

        await self._store_payloads(payloads)
        await self._write_ability(ability)
        return ability['id'], facts

    async def _store_payloads(self, payloads):
        for payload in payloads:
            for path in Path(self.repo_dir).rglob(payload):
                try:
                    shutil.copyfile(path, os.path.join(self.payloads_dir, path.name))
                except:
                    print('could not move')

    async def _save_source(self, name, facts):
        source = dict(
            id=str(uuid.uuid5(uuid.NAMESPACE_OID, name)),
            name='%s (Emu)' % name,
            facts=await self._unique_facts(facts)
        )
        await self._write_source(source)

    @staticmethod
    async def _unique_facts(facts):
        unique_facts = []
        for fact in facts:
            if fact not in unique_facts:
                unique_facts.append(fact)
        return unique_facts

    async def _write_source(self, data):
        d = os.path.join(self.data_dir, 'sources')

        if not os.path.exists(d):
            os.makedirs(d)

        file_path = os.path.join(d, '%s.yml' % data['id'])
        with open(file_path, 'w') as f:
            f.write(yaml.dump(data))
