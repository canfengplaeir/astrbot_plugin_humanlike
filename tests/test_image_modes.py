"""Offline routing tests; AstrBot and providers are stubbed, no API requests."""
import logging
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

pkg = types.ModuleType('image_test_plugin')
pkg.__path__ = [str(Path(__file__).resolve().parents[1])]
sys.modules[pkg.__name__] = pkg
api = types.ModuleType('astrbot.api')
api.logger = logging.getLogger('image-tests')
events = types.ModuleType('astrbot.api.event')
events.AstrMessageEvent = object
sys.modules.update({'astrbot': types.ModuleType('astrbot'),
                    'astrbot.api': api, 'astrbot.api.event': events})
from image_test_plugin.ai.client import AIClient
from image_test_plugin.engine.accumulator import AccumulationManager
from image_test_plugin.engine.state import GroupState


class Image:
    url = 'https://example.org/sticker.gif'


class ImageModeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.event = SimpleNamespace(
            message_obj=SimpleNamespace(message=[Image()]), message_str='这是什么？',
            unified_msg_origin='test:group:1', is_at_or_wake_command=False,
            get_sender_name=lambda: '用户',
        )
        self.ctx = SimpleNamespace(
            get_current_chat_provider_id=AsyncMock(return_value='chat'),
        )
        self.config = {'reply_engine': {'judge_provider_id': 'judge'}}
        self.ai = AIClient(self.ctx, self.config)
        self.ai._llm = AsyncMock(return_value='发言')

    async def test_disabled_never_calls_vision_or_passes_images(self):
        self.config['reply_engine']['enable_image_understanding'] = False
        urls, media = await self.ai.prepare_image_input(self.event)
        self.assertEqual((urls, media), ([], ''))
        self.ai._llm.assert_not_awaited()
        await self.ai.judge(self.event, 80, [], image_urls=urls, media_context=media)
        self.assertEqual(self.ai._llm.call_args.args[0], 'judge')
        self.assertEqual(self.ai._llm.call_args.kwargs['image_urls'], [])
        self.assertNotIn('[附带图片]', self.ai._llm.call_args.args[1])

    async def test_direct_mode_routes_original_image_to_chat(self):
        urls, media = await self.ai.prepare_image_input(self.event)
        self.ai._llm.assert_not_awaited()
        self.assertEqual(urls, [Image.url])
        await self.ai.judge(self.event, 80, [], image_urls=urls, media_context=media)
        self.assertEqual(self.ai._llm.call_args.args[0], 'chat')
        self.assertEqual(self.ai._llm.call_args.kwargs['image_urls'], [Image.url])

    async def test_description_once_then_text_judge_reply_and_buffer(self):
        self.config['reply_engine']['image_description_provider_id'] = 'vision'
        self.ai._llm.return_value = '一只猫，图片上写着你好'
        urls, media = await self.ai.prepare_image_input(self.event)
        self.assertEqual(self.ai._llm.call_args.args[0], 'vision')
        self.assertEqual(urls, [])
        state = GroupState()
        text = self.ai.message_text_with_media(self.event, image_urls=urls, media_context=media)
        AccumulationManager({}).add_to_buffer(state, self.event, text, '用户',
                                              image_urls=urls, media_context=media)
        row = state.pending_messages[0]
        self.assertIn('一只猫', row['text'])
        for method in (self.ai.judge, self.ai.reply, self.ai.judge_batch, self.ai.reply_batch):
            await method(self.event, 80, [row], image_urls=urls, media_context=media)
            self.assertEqual(self.ai._llm.call_args.kwargs['image_urls'], [])
            self.assertIn('一只猫', self.ai._llm.call_args.args[1])
        self.assertEqual([c.args[0] for c in self.ai._llm.call_args_list],
                         ['vision', 'judge', 'chat', 'judge', 'chat'])

    async def test_description_failure_does_not_forward_original(self):
        self.config['reply_engine']['image_description_provider_id'] = 'broken'
        for failure in (TimeoutError(), RuntimeError('unavailable')):
            self.ai._llm.side_effect = failure
            with self.assertLogs('image-tests'):
                urls, media = await self.ai.prepare_image_input(self.event)
            self.assertEqual(urls, [])
            self.assertIn('失败', media)
        self.ai._llm.side_effect = None
        self.ai._llm.return_value = ''
        with self.assertLogs('image-tests'):
            self.assertEqual(await self.ai.prepare_image_input(self.event), ([], '[图片转述失败]'))


if __name__ == '__main__':
    unittest.main()
