import unittest

from app.mind.recipe import step_params


class RecipeBindingTests(unittest.TestCase):
    def test_absent_optional_input_is_not_invented_and_skill_default_remains_usable(self):
        recipe = {'steps': [{'bindings': {'query': 'input.query', 'count': 'input.count'}}]}
        spec = {'input_schema': {'properties': {'query': {'type': 'string'}, 'count': {'type': 'integer'}},
                                'required': ['query']}}
        self.assertEqual(step_params(recipe, 0, {'input': {'query': 'current query'}}, spec),
                         {'query': 'current query'})
        self.assertEqual(step_params(recipe, 0, {'input': {'query': 'current query', 'count': 2}}, spec),
                         {'query': 'current query', 'count': 2})
        with self.assertRaisesRegex(ValueError, 'required value'):
            step_params(recipe, 0, {'input': {'count': 2}}, spec)

    def test_missing_upstream_output_is_not_hidden_by_optional_destination(self):
        recipe = {'steps': [{'bindings': {'count': 'step1.count'}}]}
        spec = {'input_schema': {'properties': {'count': {'type': 'integer'}}, 'required': []}}
        with self.assertRaisesRegex(ValueError, 'unavailable step output'):
            step_params(recipe, 0, {}, spec)
        with self.assertRaisesRegex(ValueError, 'required value'):
            step_params(recipe, 0, {'step1': {}}, spec)
