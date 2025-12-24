from typing import Optional
import aiohttp
import time
import base64
import hmac
import hashlib
import os
import asyncio
from openai import AsyncOpenAI

# 核心导入（对齐参考项目）
from astrbot.api import logger
from astrbot.api.star import register, Star, Context, StarTools
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import Plain, Image
from astrbot.api import llm_tool

@register("astrbot_plugin_seedream_image", "插件开发者", "使用火山方舟seedreamAPI生成图片，触发指令为画图豆包", "1.0.0")
class SeedreamImagePlugin(Star):
    def __init__(self, context: Context, config):
        super().__init__(context)
        self.config = config
        
        # 火山方舟配置（仅API Key）
        self.volc_api_key = self.config.get("VOLC_API_KEY", "")
        self.image_size = self.config.get("image_size", "1920x1920")  # 满足最低像素要求
        self.model_version = self.config.get("model_version", "seedream-v1")
        self.volc_endpoint = self.config.get("VOLC_ENDPOINT", "https://ark.cn-beijing.volces.com/api/v3")
        
        # 参考Gitee项目：记录正在生成的用户，防止重复请求
        self.processing_users = set()
        
        # 图片自动清理配置
        self.auto_clean_delay = self.config.get("auto_clean_delay", 60)  # 延迟60秒清理
        self.clean_task_timeout = self.config.get("clean_task_timeout", 10)  # 清理任务超时时间
        
        # 检查API Key配置
        if not self.volc_api_key:
            logger.warning("VOLC_API_KEY未配置，图片生成功能将无法正常使用")
        else:
            logger.info("火山方舟API Key配置完成")
        
        # 校验尺寸是否满足火山方舟最低像素要求（3686400）
        self._validate_image_size()

    def _validate_image_size(self):
        """校验图片尺寸是否满足火山方舟最低像素要求"""
        try:
            width, height = map(int, self.image_size.split("x"))
            total_pixels = width * height
            min_pixels = 3686400
            if total_pixels < min_pixels:
                self.image_size = "1920x1920"
                logger.warning(f"配置的尺寸{width}x{height}像素不足，自动改为1920x1920")
        except ValueError:
            self.image_size = "1920x1920"
            logger.error("图片尺寸格式错误，自动改为1920x1920")

    def _get_save_path(self, extension: str = ".jpg") -> str:
        """参考Gitee项目：获取图片本地保存路径"""
        # 使用AstrBot插件数据目录，避免权限问题
        base_dir = StarTools.get_data_dir("astrbot_plugin_seedream_image")
        image_dir = base_dir / "images"
        image_dir.mkdir(exist_ok=True)  # 自动创建目录
        # 生成唯一文件名
        filename = f"{int(time.time())}_{os.urandom(4).hex()}{extension}"
        return str(image_dir / filename)
    
    async def _clean_image_file(self, filepath: str):
        """异步清理图片文件"""
        # 延迟指定时间后清理
        await asyncio.sleep(self.auto_clean_delay)
        
        # 设置清理任务超时
        try:
            await asyncio.wait_for(self._do_clean_file(filepath), timeout=self.clean_task_timeout)
            logger.info(f"图片文件已自动清理：{filepath}")
        except asyncio.TimeoutError:
            logger.error(f"清理图片文件超时：{filepath}")
        except Exception as e:
            logger.error(f"清理图片文件失败：{filepath}，错误：{str(e)}", exc_info=True)
    
    async def _do_clean_file(self, filepath: str):
        """执行文件删除操作"""
        if os.path.exists(filepath):
            try:
                os.remove(filepath)
                # 检查并清理空目录（可选）
                dir_path = os.path.dirname(filepath)
                if os.path.exists(dir_path) and not os.listdir(dir_path):
                    os.rmdir(dir_path)
                    logger.info(f"空图片目录已清理：{dir_path}")
            except PermissionError:
                # 文件被占用时，尝试稍后再删
                logger.warning(f"文件被占用无法删除：{filepath}，将在10秒后重试")
                await asyncio.sleep(10)
                if os.path.exists(filepath):
                    os.remove(filepath)
            except Exception as e:
                raise e
        else:
            logger.warning(f"图片文件不存在，无需清理：{filepath}")

    async def _download_image(self, url: str) -> str:
        """参考Gitee项目：下载图片到本地并返回文件路径"""
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": "https://www.volcengine.com/",
                "Origin": "https://www.volcengine.com/"
            }
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
                async with session.get(url, headers=headers) as resp:
                    if resp.status != 200:
                        raise Exception(f"下载图片失败: HTTP {resp.status}")
                    image_data = await resp.read()
            
            # 保存到本地
            filepath = self._get_save_path()
            with open(filepath, "wb") as f:
                f.write(image_data)
            
            logger.info(f"图片已保存到本地：{filepath}")
            return filepath
        except Exception as e:
            logger.error(f"下载图片失败：{str(e)}", exc_info=True)
            raise

    async def _generate_image(self, prompt: str) -> str:
        """调用火山方舟API生成图片，返回本地文件路径"""
        if not self.volc_api_key:
            raise Exception("请先配置VOLC_API_KEY")
        
        # 初始化OpenAI兼容客户端
        client = AsyncOpenAI(
            api_key=self.volc_api_key,
            base_url=self.volc_endpoint
        )
        
        # 调用火山方舟Seedream API
        try:
            response = await client.images.generate(
                model=self.model_version,
                prompt=prompt,
                n=1,
                size=self.image_size
            )
        except Exception as e:
            error_msg = str(e)
            if "401" in error_msg:
                raise Exception("API Key无效或已过期")
            elif "429" in error_msg:
                raise Exception("API调用次数超限，请稍后再试")
            elif "400" in error_msg:
                raise Exception(f"参数错误：{error_msg}")
            else:
                raise Exception(f"API调用失败：{error_msg}")
        
        # 解析返回结果
        if not response.data or len(response.data) == 0:
            raise Exception("火山方舟API未返回图片数据")
        
        image_data = response.data[0]
        if not image_data.url:
            raise Exception("火山方舟API未返回图片URL")
        
        # 下载图片到本地（核心参考Gitee项目逻辑）
        return await self._download_image(image_data.url)

    @filter.command("画图豆包")
    async def generate_image(self, event: AstrMessageEvent, prompt: str):
        """
        火山方舟图片生成指令
        用法: 画图豆包 <提示词>
        示例: 画图豆包 星空下的大海
        """
        # 参考Gitee项目：空提示词检查
        if not prompt.strip():
            yield event.plain_result("请提供图片生成的提示词！使用方法：画图豆包 <提示词>")
            return
        
        # 参考Gitee项目：防重复请求
        user_id = event.get_sender_id()
        if user_id in self.processing_users:
            yield event.plain_result("您有正在进行的生图任务，请稍候...")
            return
        
        self.processing_users.add(user_id)
        image_path = None
        try:
            logger.info(f"收到图片生成请求，用户{user_id}，提示词：{prompt}，尺寸：{self.image_size}")
            
            # 生成图片并获取本地路径
            image_path = await self._generate_image(prompt)
            
            # 核心参考Gitee项目：使用Image.fromFileSystem + chain_result发送
            yield event.chain_result([Image.fromFileSystem(image_path)])
            logger.info(f"图片已成功发送给用户{user_id}，路径：{image_path}")
            
            # 启动异步清理任务（非阻塞）
            asyncio.create_task(
                self._clean_image_file(image_path),
                name=f"clean_image_{os.path.basename(image_path)}"
            )
            logger.info(f"已启动图片自动清理任务，将在{self.auto_clean_delay}秒后清理：{image_path}")
            
        except Exception as e:
            error_msg = f"生成图片失败：{str(e)}"
            logger.error(error_msg, exc_info=True)
            yield event.plain_result(error_msg)
            
            # 如果生成了图片但发送失败，也需要清理
            if image_path and os.path.exists(image_path):
                asyncio.create_task(self._clean_image_file(image_path))
                logger.info(f"生成失败，已启动图片清理任务：{image_path}")
        finally:
            # 参考Gitee项目：移除处理中的用户标记
            if user_id in self.processing_users:
                self.processing_users.remove(user_id)
    
    def _clean_all_images(self):
        """清理所有生成的图片文件（用于插件卸载）"""
        try:
            base_dir = StarTools.get_data_dir("astrbot_plugin_seedream_image")
            image_dir = base_dir / "images"
            
            if os.path.exists(image_dir):
                for filename in os.listdir(image_dir):
                    filepath = os.path.join(image_dir, filename)
                    if os.path.isfile(filepath):
                        os.remove(filepath)
                        logger.info(f"插件卸载，清理图片文件：{filepath}")
                
                # 删除空目录
                if not os.listdir(image_dir):
                    os.rmdir(image_dir)
                    logger.info("插件卸载，清理空图片目录")
                    
                logger.info("所有图片文件已清理完成")
        except Exception as e:
            logger.error(f"清理图片文件失败：{str(e)}", exc_info=True)

    async def terminate(self):
        """插件卸载时的清理操作"""
        # 取消所有正在运行的清理任务
        for task in asyncio.all_tasks():
            if task.get_name().startswith("clean_image_") and not task.done():
                task.cancel()
                logger.info(f"已取消清理任务：{task.get_name()}")
        
        # 同步清理所有图片文件
        self._clean_all_images()
        
        logger.info("Seedream图片生成插件已终止，所有资源已清理")