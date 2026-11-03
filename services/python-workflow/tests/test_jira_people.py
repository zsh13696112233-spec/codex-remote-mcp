import json
import unittest
from unittest.mock import AsyncMock, patch
from jira_runtime import collect_people, JiraCommandError
from jira_pool import validate_people


class PeopleTests(unittest.IsolatedAsyncioTestCase):
    async def read(self, responses):
        with patch('jira_runtime.powershell', AsyncMock(side_effect=responses)) as command:
            result = await collect_people(None, b'{}', 'bot', 'C:\\work', {'TEST-1': '经办人'})
            return result['TEST-1'], command

    async def test_multiple_names_and_single_field_query(self):
        value, command = await self.read([
            json.dumps([{'id': 'customfield_12', 'name': '开发人'}]),
            json.dumps({'fields': {'customfield_12': [{'displayName': '甲'}, {'displayName': '乙'}]}})])
        self.assertEqual(value['developers'], ['甲', '乙'])
        self.assertEqual(value['assignee'], '经办人')
        validate_people(value)
        code = command.call_args_list[1].args[1]
        self.assertIn("--fields 'customfield_12'", code)
        self.assertNotIn('--full', code)
        self.assertNotIn('assignee', code)

    async def test_empty_missing_and_invalid_people(self):
        for fields, status in [({'customfield_12': None}, 'empty'), ({}, 'unavailable'),
                               ({'customfield_12': {'name': 'not-a-display-name'}}, 'unavailable')]:
            value, _ = await self.read([json.dumps([{'id': 'customfield_12', 'name': '开发人'}]), json.dumps({'fields': fields})])
            self.assertEqual(value['developerStatus'], status)
            self.assertEqual(value['developers'], [])

    async def test_known_failure_does_not_retry(self):
        value, command = await self.read([json.dumps([{'id': 'customfield_12', 'name': '开发人'}]), JiraCommandError('failed')])
        self.assertEqual(value['developerStatus'], 'unavailable')
        self.assertEqual(command.await_count, 2)

    async def test_unknown_stop_propagates(self):
        with self.assertRaises(RuntimeError):
            await self.read([RuntimeError('unknown stop')])

    async def test_ambiguous_mapping_is_not_guessed(self):
        value, command = await self.read([json.dumps([{'id': 'customfield_1', 'name': '开发人'}, {'id': 'customfield_2', 'name': '开发人'}])])
        self.assertEqual(value['developerStatus'], 'unavailable')
        self.assertEqual(command.await_count, 1)
