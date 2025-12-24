import os
import time
import re
import json
import uuid
import aiohttp
import asyncio
from urllib.parse import urlparse, quote, unquote

# 核心导入
from astrbot.api import logger
from astrbot.api.star import register, Star, Context, StarTools
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import Plain, Image, Reply

# 插件常量定义
PLUGIN_NAME = "astrbot_plugin_seedream_image"
# 火山方舟最低像素要求（3686400 = 1920x1920）
MIN_PIXELS = 3686400

@register(PLUGIN_NAME, "插件开发者", "火山方舟Seedream图片生成（文生图/图生图）", "3.2.0")
class SeedreamImagePlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        
        # 1. 解析配置文件
        self.api_key = config.get("VOLC_API_KEY", "").strip()
        self.api_endpoint = config.get("VOLC_ENDPOINT", "https://ark.cn-beijing.volces.com/api/v3").strip()
        self.image_size = config.get("image_size", "4096x4096").strip()
        self.model_version = config.get("model_version", "seedream-v1").strip()
        
        # 2. 校验并处理图片尺寸
        self.valid_size, self.size_error = self._validate_image_size(self.image_size)
        if self.size_error:
            logger.warning(f"[{PLUGIN_NAME}] 尺寸配置异常：{self.size_error}，已自动调整为 1920x1920")
            self.valid_size = "1920x1920"
        
        # 3. 拼接完整API地址
        self.full_api_url = f"{self.api_endpoint.rstrip('/')}/images/generations"
        
        # 4. 限流/防重配置
        self.rate_limit_seconds = 10.0
        self.processing_users = set()
        self.last_operations = {}
        
        # 5. 文件清理配置
        self.retention_hours = float(config.get("auto_clean_delay", 1.0) / 3600) if config.get("auto_clean_delay") else 1.0
        self.last_cleanup_time = 0

        # 6. 核心配置校验
        if not self.api_key:
            logger.error(f"[{PLUGIN_NAME}] VOLC_API_KEY未配置！请填写火山方舟账号的API KEY")
        logger.info(f"[{PLUGIN_NAME}] 初始化完成 | 模型版本：{self.model_version} | 生成尺寸：{self.valid_size} | API端点：{self.full_api_url}")

    # =========================================================
    # 尺寸校验工具
    # =========================================================
    def _validate_image_size(self, size_str: str) -> tuple:
        """校验图片尺寸是否符合火山方舟要求"""
        size_pattern = re.compile(r'^(\d+)x(\d+)$', re.IGNORECASE)
        match = size_pattern.match(size_str)
        
        if not match:
            return "1920x1920", f"尺寸格式错误（{size_str}），需为WxH格式"
        
        width = int(match.group(1))
        height = int(match.group(2))
        total_pixels = width * height
        
        if total_pixels < MIN_PIXELS:
            return "1920x1920", f"像素总数不足（{total_pixels} < {MIN_PIXELS}）"
        
        if width > 8192 or height > 8192:
            return "4096x4096", f"边长过大（{width}x{height}），已调整为4096x4096"
        
        return size_str, ""

    # =========================================================
    # 通用工具方法
    # =========================================================
    def _cleanup_temp_files(self):
        """自动清理过期图片文件"""
        if self.retention_hours <= 0:
            return
            
        now = time.time()
        if now - self.last_cleanup_time < 3600:
            return

        save_dir = StarTools.get_data_dir(PLUGIN_NAME) / "images"
        if not save_dir.exists():
            return

        retention_seconds = self.retention_hours * 3600
        deleted_count = 0

        try:
            for filename in os.listdir(save_dir):
                file_path = save_dir / filename
                if file_path.is_file() and now - file_path.stat().st_mtime > retention_seconds:
                    try:
                        os.remove(file_path)
                        deleted_count += 1
                    except Exception as del_err:
                        logger.warning(f"[{PLUGIN_NAME}] 删除过期文件失败 {filename}: {del_err}")
            
            if deleted_count > 0:
                logger.info(f"[{PLUGIN_NAME}] 清理完成，共删除 {deleted_count} 张过期图片")
            
            self.last_cleanup_time = now
            
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 自动清理流程异常: {e}")

    async def _download_generated_image(self, url: str) -> str:
        """下载API生成的图片"""
        self._cleanup_temp_files()
        
        if not url or not url.startswith("http"):
            raise Exception("无效的图片URL")
        
        url = unquote(url)
        url = quote(url, safe=':/?&=')
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": urlparse(self.api_endpoint).netloc or "https://ark.cn-beijing.volces.com/"
        }
        
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=60),
                connector=aiohttp.TCPConnector(ssl=False)
            ) as session:
                async with session.get(
                    url, 
                    headers=headers,
                    allow_redirects=True
                ) as resp:
                    if resp.status != 200:
                        raise Exception(f"下载失败 [HTTP {resp.status}]")
                    image_data = await resp.read()
            
            # 保存图片
            save_dir = StarTools.get_data_dir(PLUGIN_NAME) / "images"
            save_dir.mkdir(parents=True, exist_ok=True)
            
            file_name = f"seedream_{int(time.time())}_{uuid.uuid4().hex[:8]}.jpg"
            save_path = save_dir / file_name
            
            with open(save_path, "wb") as f:
                f.write(image_data)
                
            return str(save_path)
            
        except Exception as e:
            raise Exception(f"图片下载失败: {str(e)}")

    def _extract_image_url_list(self, event: AstrMessageEvent) -> list:
        """提取消息中的图片URL列表"""
        image_urls = []
        
        if hasattr(event, 'message_obj') and event.message_obj and event.message_obj.message:
            for component in event.message_obj.message:
                if isinstance(component, Image):
                    img_url = ""
                    if hasattr(component, 'url') and component.url:
                        img_url = component.url.strip()
                    elif hasattr(component, 'file_id') and component.file_id:
                        file_id = component.file_id.replace("/", "_")
                        img_url = f"https://gchat.qpic.cn/gchatpic_new/0/0-0-{file_id}/0?tp=webp&wxfrom=5&wx_lazy=1"
                    
                    if img_url and img_url not in image_urls:
                        image_urls.append(img_url)
        
        return image_urls

    # =========================================================
    # 核心API调用逻辑
    # =========================================================
    async def _call_seedream_api(self, prompt: str, image_urls: list = None):
        """调用火山方舟Seedream API"""
        if not self.api_key:
            raise Exception("VOLC_API_KEY未配置")
        
        # 构建基础请求体
        payload = {
            "model": self.model_version,
            "prompt": prompt.strip() or "高质量高清图片",
            "size": self.valid_size,
            "watermark": False
        }
        
        # 图生图参数
        if image_urls and len(image_urls) > 0:
            payload["image"] = image_urls
        
        # 请求头
        headers = {
            'Authorization': f'Bearer {self.api_key}',
            'Content-Type': 'application/json',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }
        
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as session:
                async with session.post(
                    self.full_api_url,
                    headers=headers,
                    json=payload,
                    ssl=False
                ) as resp:
                    response_text = await resp.text()
                    
                    # 处理错误响应
                    if resp.status != 200:
                        try:
                            error_data = json.loads(response_text)
                            error_msg = error_data.get("error", {}).get("message", f"请求失败 [HTTP {resp.status}]")
                            error_code = error_data.get("error", {}).get("code", "")
                            
                            if error_code == "InvalidParameter":
                                error_msg = f"参数错误：{error_msg}"
                            elif error_code == "Unauthorized":
                                error_msg = "API KEY无效或未授权"
                                
                        except:
                            error_msg = f"API请求失败 [HTTP {resp.status}]：{response_text[:200]}"
                        raise Exception(error_msg)
                    
                    # 解析成功响应
                    try:
                        response_data = json.loads(response_text)
                        if "data" in response_data and len(response_data["data"]) > 0:
                            generated_url = response_data["data"][0].get("url")
                            if not generated_url:
                                raise Exception("API返回无图片URL")
                            return generated_url
                        else:
                            raise Exception(f"响应格式异常：{str(response_data)[:200]}")
                    except Exception as e:
                        raise Exception(f"响应解析失败：{str(e)} | 原始响应：{response_text[:200]}")
        
        except Exception as e:
            error_msg = str(e)
            if "429" in error_msg:
                raise Exception("调用频率超限，请稍后再试")
            elif "403" in error_msg:
                raise Exception("API KEY无使用权限")
            else:
                raise Exception(f"API调用失败：{error_msg}")

    # =========================================================
    # 指令处理（精简输出）
    # =========================================================
    @filter.command("画图豆包")
    async def generate_image(self, event: AstrMessageEvent, prompt: str = ""):
        """
        火山方舟Seedream图片生成
        使用方法：
        1. 文生图：画图豆包 <提示词>
        2. 图生图：画图豆包 <提示词> + 发送图片
        """
        # 提取完整提示词
        full_text = ""
        if hasattr(event, 'message_obj') and event.message_obj and event.message_obj.message:
            for component in event.message_obj.message:
                if isinstance(component, Plain):
                    full_text += component.text
        
        if not full_text:
            full_text = prompt
        
        # 移除指令关键词
        real_prompt = re.sub(r"画图豆包", "", full_text).strip()
        
        # 提取图片URL列表
        image_urls = self._extract_image_url_list(event)
        
        # 基础校验
        user_id = event.get_sender_id()
        
        # 防抖检查
        current_time = time.time()
        if user_id in self.last_operations:
            if current_time - self.last_operations[user_id] < self.rate_limit_seconds:
                yield event.plain_result("操作过快，请稍后再试")
                return
        self.last_operations[user_id] = current_time
        
        # 防重复处理
        if user_id in self.processing_users:
            yield event.plain_result("有正在进行的生图任务，请稍候")
            return
        
        # 无提示词且无图片
        if not real_prompt and not image_urls:
            yield event.plain_result("请提供提示词或图片")
            return
        
        # 开始生成
        self.processing_users.add(user_id)
        try:
            # 精简的状态提示
            if image_urls:
                yield event.plain_result("开始图生图...")
            else:
                yield event.plain_result("开始文生图...")
            
            # 调用API
            generated_url = await self._call_seedream_api(real_prompt, image_urls)
            
            # 下载图片（无提示）
            local_path = await self._download_generated_image(generated_url)
            
            # 构造回复（精简结果）
            reply_components = []
            if hasattr(event.message_obj, 'message_id'):
                reply_components.append(Reply(id=event.message_obj.message_id))
            
            reply_components.extend([
                Image.fromFileSystem(local_path),
                Plain(text=f"生成完成\n提示词：{real_prompt or '纯图生图'}")
            ])
            
            yield event.chain_result(reply_components)
            
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 生图失败（用户{user_id}）: {str(e)}")
            yield event.plain_result(f"生成失败：{str(e)}")
            
        finally:
            if user_id in self.processing_users:
                self.processing_users.remove(user_id)

    # =========================================================
    # 插件卸载清理
    # =========================================================
    async def terminate(self):
        """清理所有生成的图片"""
        save_dir = StarTools.get_data_dir(PLUGIN_NAME) / "images"
        if save_dir.exists():
            for filename in os.listdir(save_dir):
                file_path = save_dir / filename
                if file_path.is_file():
                    try:
                        os.remove(file_path)
                    except:
                        pass
            try:
                os.rmdir(save_dir)
            except:
                pass
        
        logger.info(f"[{PLUGIN_NAME}] 插件已卸载，所有图片文件已清理")
