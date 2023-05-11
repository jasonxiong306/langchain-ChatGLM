import argparse
import json
import os
import shutil
import subprocess
import tempfile
import requests
import asyncio
from typing import List, Optional, Dict, Any

import nltk
import pydantic
import uvicorn
from fastapi import Body, FastAPI, File, Form, Query, UploadFile, WebSocket, Request
from fastapi.openapi.utils import get_openapi
from pydantic import BaseModel
from typing_extensions import Annotated
from starlette.responses import RedirectResponse
from chains.local_doc_qa import LocalDocQA
from configs.model_config import (VS_ROOT_PATH, EMBEDDING_DEVICE, EMBEDDING_MODEL, LLM_MODEL, UPLOAD_ROOT_PATH,
                                  NLTK_DATA_PATH, VECTOR_SEARCH_TOP_K, LLM_HISTORY_LEN)

nltk.data.path = [NLTK_DATA_PATH] + nltk.data.path


class BaseResponse(BaseModel):
    code: int = pydantic.Field(200, description="HTTP status code")
    msg: str = pydantic.Field("success", description="HTTP status message")

    class Config:
        schema_extra = {
            "example": {
                "code": 200,
                "msg": "success",
            }
        }


class ListDocsResponse(BaseResponse):
    data: List[str] = pydantic.Field(..., description="List of document names")

    class Config:
        schema_extra = {
            "example": {
                "code": 200,
                "msg": "success",
                "data": ["doc1.docx", "doc2.pdf", "doc3.txt"],
            }
        }


class ChatMessage(BaseModel):
    question: str = pydantic.Field(..., description="Question text")
    response: str = pydantic.Field(..., description="Response text")
    history: List[List[str]] = pydantic.Field(..., description="History text")
    source_documents: List[str] = pydantic.Field(
        ..., description="List of source documents and their scores"
    )

    class Config:
        schema_extra = {
            "example": {
                "question": "工伤保险如何办理？",
                "response": "根据已知信息，可以总结如下：\n\n1. 参保单位为员工缴纳工伤保险费，以保障员工在发生工伤时能够获得相应的待遇。\n2. 不同地区的工伤保险缴费规定可能有所不同，需要向当地社保部门咨询以了解具体的缴费标准和规定。\n3. 工伤从业人员及其近亲属需要申请工伤认定，确认享受的待遇资格，并按时缴纳工伤保险费。\n4. 工伤保险待遇包括工伤医疗、康复、辅助器具配置费用、伤残待遇、工亡待遇、一次性工亡补助金等。\n5. 工伤保险待遇领取资格认证包括长期待遇领取人员认证和一次性待遇领取人员认证。\n6. 工伤保险基金支付的待遇项目包括工伤医疗待遇、康复待遇、辅助器具配置费用、一次性工亡补助金、丧葬补助金等。",
                "history": [
                    [
                        "工伤保险是什么？",
                        "工伤保险是指用人单位按照国家规定，为本单位的职工和用人单位的其他人员，缴纳工伤保险费，由保险机构按照国家规定的标准，给予工伤保险待遇的社会保险制度。",
                    ]
                ],
                "source_documents": [
                    "出处 [1] 广州市单位从业的特定人员参加工伤保险办事指引.docx：\n\n\t( 一)  从业单位  (组织)  按“自愿参保”原则，  为未建 立劳动关系的特定从业人员单项参加工伤保险 、缴纳工伤保 险费。",
                    "出处 [2] ...",
                    "出处 [3] ...",
                ],
            }
        }


def get_folder_path(local_doc_id: str):
    return os.path.join(UPLOAD_ROOT_PATH, local_doc_id)


def get_vs_path(local_doc_id: str):
    return os.path.join(VS_ROOT_PATH, local_doc_id)


def get_file_path(local_doc_id: str, doc_name: str):
    return os.path.join(UPLOAD_ROOT_PATH, local_doc_id, doc_name)


async def single_upload_file(
        file: UploadFile = File(description="A single binary file"),
        knowledge_base_id: str = Form(..., description="Knowledge Base Name", example="kb1"),
):
    saved_path = get_folder_path(knowledge_base_id)
    if not os.path.exists(saved_path):
        os.makedirs(saved_path)

    file_content = await file.read()  # 读取上传文件的内容

    file_path = os.path.join(saved_path, file.filename)
    if os.path.exists(file_path) and os.path.getsize(file_path) == len(file_content):
        file_status = f"文件 {file.filename} 已存在。"
        return BaseResponse(code=200, msg=file_status)

    with open(file_path, "wb") as f:
        f.write(file_content)

    vs_path = get_vs_path(knowledge_base_id)
    if os.path.exists(vs_path):
        added_files = await local_doc_qa.add_files_to_knowledge_vector_store(vs_path, [file_path])
        if len(added_files) > 0:
            file_status = f"文件 {file.filename} 已上传并已加载知识库，请开始提问。"
            return BaseResponse(code=200, msg=file_status)
    else:
        vs_path, loaded_files = await local_doc_qa.init_knowledge_vector_store([file_path], vs_path)
        if len(loaded_files) > 0:
            file_status = f"文件 {file.filename} 已上传至新的知识库，并已加载知识库，请开始提问。"
            return BaseResponse(code=200, msg=file_status)

    file_status = "文件上传失败，请重新上传"
    return BaseResponse(code=500, msg=file_status)


async def upload_file(
        files: Annotated[
            List[UploadFile], File(description="Multiple files as UploadFile")
        ],
        knowledge_base_id: str = Form(..., description="Knowledge Base Name", example="kb1"),
):
    saved_path = get_folder_path(knowledge_base_id)
    if not os.path.exists(saved_path):
        os.makedirs(saved_path)
    filelist = []
    for file in files:
        file_content = ''
        file_path = os.path.join(saved_path, file.filename)
        file_content = file.file.read()
        if os.path.exists(file_path) and os.path.getsize(file_path) == len(file_content):
            continue
        with open(file_path, "ab+") as f:
            f.write(file_content)
        filelist.append(file_path)
    if filelist:
        vs_path, loaded_files = local_doc_qa.init_knowledge_vector_store(filelist, get_vs_path(knowledge_base_id))
        if len(loaded_files):
            file_status = f"已上传 {'、'.join([os.path.split(i)[-1] for i in loaded_files])} 至知识库，并已加载知识库，请开始提问"
            return BaseResponse(code=200, msg=file_status)
    file_status = "文件未成功加载，请重新上传文件"
    return BaseResponse(code=500, msg=file_status)


async def list_docs(
        knowledge_base_id: Optional[str] = Query(description="Knowledge Base Name", example="kb1")
):
    if knowledge_base_id:
        local_doc_folder = get_folder_path(knowledge_base_id)
        if not os.path.exists(local_doc_folder):
            return {"code": 1, "msg": f"Knowledge base {knowledge_base_id} not found"}
        all_doc_names = [
            doc
            for doc in os.listdir(local_doc_folder)
            if os.path.isfile(os.path.join(local_doc_folder, doc))
        ]
        return ListDocsResponse(data=all_doc_names)
    else:
        if not os.path.exists(UPLOAD_ROOT_PATH):
            all_doc_ids = []
        else:
            all_doc_ids = [
                folder
                for folder in os.listdir(UPLOAD_ROOT_PATH)
                if os.path.isdir(os.path.join(UPLOAD_ROOT_PATH, folder))
            ]

        return ListDocsResponse(data=all_doc_ids)


async def delete_docs(
        knowledge_base_id: str = Form(...,
                                      description="Knowledge Base Name(注意此方法仅删除上传的文件并不会删除知识库(FAISS)内数据)",
                                      example="kb1"),
        doc_name: Optional[str] = Form(
            None, description="doc name", example="doc_name_1.pdf"
        ),
):
    if not os.path.exists(os.path.join(UPLOAD_ROOT_PATH, knowledge_base_id)):
        return {"code": 1, "msg": f"Knowledge base {knowledge_base_id} not found"}
    if doc_name:
        doc_path = get_file_path(knowledge_base_id, doc_name)
        if os.path.exists(doc_path):
            os.remove(doc_path)
        else:
            return {"code": 1, "msg": f"document {doc_name} not found"}

        remain_docs = await list_docs(knowledge_base_id)
        if remain_docs["code"] != 0 or len(remain_docs["data"]) == 0:
            shutil.rmtree(get_folder_path(knowledge_base_id), ignore_errors=True)
        else:
            local_doc_qa.init_knowledge_vector_store(
                get_folder_path(knowledge_base_id), get_vs_path(knowledge_base_id)
            )
    else:
        shutil.rmtree(get_folder_path(knowledge_base_id))
    return BaseResponse()


async def chat(
        knowledge_base_id: str = Body(..., description="Knowledge Base Name", example="kb1"),
        question: str = Body(..., description="Question", example="工伤保险是什么？"),
        history: List[List[str]] = Body(
            [],
            description="History of previous questions and answers",
            example=[
                [
                    "工伤保险是什么？",
                    "工伤保险是指用人单位按照国家规定，为本单位的职工和用人单位的其他人员，缴纳工伤保险费，由保险机构按照国家规定的标准，给予工伤保险待遇的社会保险制度。",
                ]
            ],
        ),
):
    # vs_path = os.path.join(VS_ROOT_PATH, knowledge_base_id)
    vs_path = "/root/github/langchain-ChatGLM/vector_store/test_case_v5"
    if not os.path.exists(vs_path):
        raise ValueError(f"Knowledge base {knowledge_base_id} not found")

    for resp, history in local_doc_qa.get_knowledge_based_answer(
            query=question, vs_path=vs_path, chat_history=history, streaming=True
    ):
        pass
    source_documents = [
        f"""出处 [{inum + 1}] {os.path.split(doc.metadata['source'])[-1]}：\n\n{doc.page_content}\n\n"""
        f"""相关度：{doc.metadata['score']}\n\n"""
        for inum, doc in enumerate(resp["source_documents"])
    ]

    return ChatMessage(
        question=question,
        response=resp["result"],
        history=history,
        source_documents=source_documents,
    )


async def no_knowledge_chat(
        question: str = Body(..., description="Question", example="工伤保险是什么？"),
        history: List[List[str]] = Body(
            [],
            description="History of previous questions and answers",
            example=[
                [
                    "工伤保险是什么？",
                    "工伤保险是指用人单位按照国家规定，为本单位的职工和用人单位的其他人员，缴纳工伤保险费，由保险机构按照国家规定的标准，给予工伤保险待遇的社会保险制度。",
                ]
            ],
        ),
):
    for resp, history in local_doc_qa._call(
            query=question, chat_history=history, streaming=True
    ):
        pass


async def stream_chat(websocket: WebSocket, knowledge_base_id: str):
    await websocket.accept()
    vs_path = os.path.join(VS_ROOT_PATH, knowledge_base_id)

    if not os.path.exists(vs_path):
        await websocket.send_json({"error": f"Knowledge base {knowledge_base_id} not found"})
        await websocket.close()
        return

    history = []
    turn = 1
    while True:
        question = await websocket.receive_text()
        await websocket.send_json({"question": question, "turn": turn, "flag": "start"})

        last_print_len = 0
        for resp, history in local_doc_qa.get_knowledge_based_answer(
                query=question, vs_path=vs_path, chat_history=history, streaming=True
        ):
            await websocket.send_text(resp["result"][last_print_len:])
            last_print_len = len(resp["result"])

        source_documents = [
            f"""出处 [{inum + 1}] {os.path.split(doc.metadata['source'])[-1]}：\n\n{doc.page_content}\n\n"""
            f"""相关度：{doc.metadata['score']}\n\n"""
            for inum, doc in enumerate(resp["source_documents"])
        ]

        await websocket.send_text(
            json.dumps(
                {
                    "question": question,
                    "turn": turn,
                    "flag": "end",
                    "sources_documents": source_documents,
                },
                ensure_ascii=False,
            )
        )
        turn += 1


async def document():
    return RedirectResponse(url="/docs")


class FeiShu(BaseModel):
    challenge: Optional[str] = None
    type: Optional[str] = None
    token: Optional[str] = None
    header: Dict[str, Any] = None
    event: Dict[str, Any] = None


# {"challenge":"30c1bad8-65df-47f0-9e83-30790cc93153","type":"url_verification","token":"RzUm7DyopWiAdKDFCoQF5d8xAWvKzOkJ"}
def feishu_auth():
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    resq = {"app_id": "cli_a4d637b299fe900e", "app_secret": "7xSLLCWjmB8kPecaLv6oEfD4yMPQiPwD"}
    resp = requests.post(url, data=resq)
    print(resp.text)
    return json.loads(resp.text)["tenant_access_token"]


async def feishu_event_async(fei_shu: FeiShu):
    print("feishu_event_async start")
    print("event:", json.dumps(fei_shu.event))
    print("header:", json.dumps(fei_shu.header))
    query = json.loads(fei_shu.event["message"]["content"])["text"]
    print("query:", query)
    vs_path = "/root/github/langchain-ChatGLM/vector_store/test_case_v5"
    if not os.path.exists(vs_path):
        raise ValueError(f"Knowledge base {vs_path} not found")
    history = []
    for resp, history in local_doc_qa.get_knowledge_based_answer(
            query=query, vs_path=vs_path, chat_history=history, streaming=True
    ):
        pass

    message_id = fei_shu.event["message"]["message_id"]
    url = "https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reply".format(message_id=message_id)
    content = "{\"text\":\"" +resp["result"].replace("\n","\\n")+"\"}"
    replay = {
        "msg_type": "text",
        "content": content
    }

    print("url and replay: ", url, replay)
    tenant_access_token = feishu_auth()
    resp = requests.post(url, data=replay, headers={"Authorization": "Bearer " + tenant_access_token})
    print("feishu resp:", resp.text)
    print("feishu_event_async end")

async def feishu_event(fei_shu: FeiShu):
    print("feishu_event start")
    asyncio.create_task(feishu_event_async(fei_shu))
    print("feishu_event end")
    return fei_shu



def main():
    global app
    global local_doc_qa
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", type=str, default="0.0.0.0")
    parser.add_argument("--port", type=int, default=6006)
    args = parser.parse_args()

    app = FastAPI()
    app.websocket("/chat-docs/stream-chat/{knowledge_base_id}")(stream_chat)
    app.post("/chat-docs/chat", response_model=ChatMessage)(chat)
    app.post("/chat-docs/chatno", response_model=ChatMessage)(no_knowledge_chat)
    app.post("/chat-docs/upload", response_model=BaseResponse)(upload_file)
    app.post("/chat-docs/uploadone", response_model=BaseResponse)(single_upload_file)
    app.get("/chat-docs/list", response_model=ListDocsResponse)(list_docs)
    app.delete("/chat-docs/delete", response_model=BaseResponse)(delete_docs)
    app.get("/", response_model=BaseResponse)(document)
    app.post("/feishu/event", response_model=FeiShu)(feishu_event)

    local_doc_qa = LocalDocQA()
    local_doc_qa.init_cfg(
        llm_model=LLM_MODEL,
        embedding_model=EMBEDDING_MODEL,
        embedding_device=EMBEDDING_DEVICE,
        llm_history_len=LLM_HISTORY_LEN,
        top_k=VECTOR_SEARCH_TOP_K,
    )
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
