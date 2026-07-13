"""
开发Agent - 负责编写、修改和优化代码
继承自BaseAgent，专注于软件开发任务
"""

import os
import re
import time
import json
import subprocess
from typing import Dict, Any, Optional, List

# 尝试用 google.generativeai 直接构造 content parts，避免 SDK 对 role="system" 的限制
try:
    import google.generativeai as genai
    from google.generativeai.types import ContentDict, PartDict
    HAS_GENAI = True
except ImportError:
    genai = None
    ContentDict = None
    PartDict = None
    HAS_GENAI = False

from .base_agent import BaseAgent

MAX_ITERATIONS = 200
WORKSPACE_DIR = "workspace"


class DevAgent(BaseAgent):
    """开发Agent，专注于代码编写、修改和优化"""
    
    def __init__(self, api_key: str, model_name: str = "gemini-2.5-flash", 
                 api_base: Optional[str] = None, tools: Optional[List] = None,
                 system_prompt: Optional[str] = None):
        """
        初始化开发Agent
        
        Args:
            api_key: API密钥
            model_name: 模型名称
            api_base: API基础URL
            tools: 可用的工具列表
            system_prompt: 自定义系统提示
        """
        if system_prompt is None:
            system_prompt = """你是一个专业的软件开发助手，擅长编写、修改和优化代码。
你的职责包括：
1. 编写高质量、可维护的代码
2. 分析和修改现有代码
3. 提供技术建议和最佳实践
4. 调试和修复代码问题
5. 优化代码性能

在工作时，请：
- 先理解需求再动手
- 保持代码简洁清晰
- 添加适当的注释
- 考虑边界情况和错误处理
- 遵循项目的编码规范"""
        
        super().__init__(api_key, model_name, api_base, tools, system_prompt)
    
    def generate_code(self, prompt: str, context: Optional[str] = None) -> str:
        """
        生成代码
        
        Args:
            prompt: 代码生成提示
            context: 上下文代码（可选）
            
        Returns:
            生成的代码
        """
        full_prompt = f"请根据以下需求生成代码：\n\n{prompt}"
        if context:
            full_prompt += f"\n\n上下文代码：\n```\n{context}\n```"
        
        return self.run(full_prompt)
    
    def review_code(self, code: str, requirements: Optional[str] = None) -> str:
        """
        代码审查
        
        Args:
            code: 要审查的代码
            requirements: 需求描述（可选）
            
        Returns:
            审查意见
        """
        prompt = f"请审查以下代码"
        if requirements:
            prompt += f"，确保它满足这些需求：{requirements}"
        prompt += f"：\n\n```\n{code}\n```"
        
        return self.run(prompt)
    
    def modify_code(self, code: str, modification_request: str) -> str:
        """
        修改代码
        
        Args:
            code: 原始代码
            modification_request: 修改请求
            
        Returns:
            修改后的代码
        """
        prompt = f"""请根据以下修改请求修改代码：

修改请求：{modification_request}

原始代码：
```
{code}
```

请直接返回修改后的完整代码，不需要额外解释。"""
        
        return self.run(prompt)
    
    def debug_code(self, code: str, error_message: str) -> str:
        """
        调试代码
        
        Args:
            code: 有问题的代码
            error_message: 错误信息
            
        Returns:
            修复后的代码
        """
        prompt = f"""请修复以下代码的错误：

错误信息：{error_message}

代码：
```
{code}
```

请分析错误原因并返回修复后的完整代码。"""
        
        return self.run(prompt)
    
    def optimize_code(self, code: str, optimization_goal: str = "性能") -> str:
        """
        优化代码
        
        Args:
            code: 要优化的代码
            optimization_goal: 优化目标
            
        Returns:
            优化后的代码
        """
        prompt = f"""请优化以下代码，优化目标：{optimization_goal}

原始代码：
```
{code}
```

请返回优化后的完整代码，并说明做了哪些优化。"""
        
        return self.run(prompt)
    
    def _get_workspace_path(self) -> str:
        """获取工作目录路径"""
        return os.path.join(os.getcwd(), WORKSPACE_DIR)
    
    def _ensure_workspace(self) -> str:
        """确保工作目录存在"""
        workspace = self._get_workspace_path()
        os.makedirs(workspace, exist_ok=True)
        return workspace
    
    def list_workspace_files(self) -> List[str]:
        """列出工作目录中的文件"""
        workspace = self._get_workspace_path()
        if not os.path.exists(workspace):
            return []
        return [f for f in os.listdir(workspace) if os.path.isfile(os.path.join(workspace, f))]
    
    def save_to_workspace(self, filename: str, content: str) -> str:
        """
        保存文件到工作目录
        
        Args:
            filename: 文件名
            content: 文件内容
            
        Returns:
            保存的文件路径
        """
        workspace = self._ensure_workspace()
        filepath = os.path.join(workspace, filename)
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)
        return filepath
    
    def read_from_workspace(self, filename: str) -> Optional[str]:
        """
        从工作目录读取文件
        
        Args:
            filename: 文件名
            
        Returns:
            文件内容，如果文件不存在则返回None
        """
        filepath = os.path.join(self._get_workspace_path(), filename)
        if not os.path.exists(filepath):
            return None
        with open(filepath, 'r', encoding='utf-8') as f:
            return f.read()
    
    def execute_python(self, filename: str) -> Dict[str, Any]:
        """
        执行工作目录中的Python文件
        
        Args:
            filename: Python文件名
            
        Returns:
            包含执行结果的字典
        """
        filepath = os.path.join(self._get_workspace_path(), filename)
        if not os.path.exists(filepath):
            return {
                "success": False,
                "error": f"文件不存在: {filepath}",
                "stdout": "",
                "stderr": ""
            }
        
        try:
            result = subprocess.run(
                ["python", filepath],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=self._get_workspace_path()
            )
            return {
                "success": result.returncode == 0,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "returncode": result.returncode
            }
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "error": "执行超时（30秒）",
                "stdout": "",
                "stderr": ""
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "stdout": "",
                "stderr": ""
            }
    
    def run(self, task: str, **kwargs) -> str:
        """
        运行开发Agent
        
        Args:
            task: 任务描述
            **kwargs: 额外参数
            
        Returns:
            Agent的响应
        """
        # 开发Agent可以添加额外的上下文
        workspace_files = self.list_workspace_files()
        if workspace_files:
            task += f"\n\n注意：工作目录中有以下文件可用：{', '.join(workspace_files)}"
        
        return super().run(task, **kwargs)
    
    def process_task(self, task: str, files: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """
        处理开发任务
        
        Args:
            task: 任务描述
            files: 相关文件字典 {文件名: 内容}
            
        Returns:
            任务处理结果
        """
        result = {
            "task": task,
            "response": None,
            "files_created": [],
            "files_modified": [],
            "error": None
        }
        
        try:
            # 如果有提供的文件，先保存
            if files:
                for filename, content in files.items():
                    self.save_to_workspace(filename, content)
                    result["files_created"].append(filename)
            
            # 执行任务
            response = self.run(task)
            result["response"] = response
            
        except Exception as e:
            result["error"] = str(e)
        
        return result