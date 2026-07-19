"""确定性生成评测语料与 Golden Dataset。

设计：
- 48 个「事实簇」：每簇一段微信风格对话，事实由 target（小雨）说出，
  携带事实的消息标记为 gold；每簇派生 3 条模拟用户提问的 golden query。
- 若干闲聊对话作为检索噪声，其中部分刻意含有与事实簇相近的泛化词汇
  （如泛泛提到奶茶/电影），提高检索难度。
- 固定随机种子，任何人重跑生成的 corpus.jsonl / golden.jsonl 逐字节一致。

运行：python -m evals.build_corpus
"""

import json
import random
from dataclasses import dataclass
from datetime import datetime, timedelta

from evals.dataset import CORPUS_PATH, GOLDEN_PATH, EVAL_DATA_DIR

SEED = 42
TARGET_NAME = "小雨"
USER_NAME = "我"

# 生成评测用的极简人设 prompt，供 generation_eval 复用（模拟 SKILL.md 的角色）
PERSONA_PROMPT = (
    "你是「小雨」的数字镜像，在微信上和前任聊天。"
    "小雨：95后女生，视觉传达设计专业，说话短句、活泼、爱用语气词。"
    "回答只依据提供的「聊天记录检索结果」中的信息；检索结果里没有的具体事实，"
    "不要编造，可以自然地说不记得了。"
)


@dataclass(frozen=True)
class FactCluster:
    fid: str
    category: str
    fact: str
    # (speaker, text, is_gold)，speaker 为 "me" / "target"
    dialogue: tuple[tuple[str, str, bool], ...]
    queries: tuple[str, ...]


FACT_CLUSTERS: list[FactCluster] = [
    # --- 偏好 ---
    FactCluster(
        "milktea",
        "偏好",
        "最喜欢的奶茶是乌龙玛奇朵，三分糖去冰",
        (
            ("me", "下午去买奶茶，给你带一杯？", False),
            ("target", "要要要！老规矩，乌龙玛奇朵，三分糖去冰", True),
            ("me", "每次都这个，不腻吗", False),
            ("target", "别的喝不惯嘛，就这杯最好喝", True),
        ),
        ("你最喜欢喝什么奶茶来着", "帮你点奶茶的话要选什么口味", "你奶茶一般几分糖"),
    ),
    FactCluster(
        "coffee",
        "偏好",
        "只喝冰美式，讨厌拿铁的奶味",
        (
            ("me", "楼下新开了家咖啡店，请你喝拿铁？", False),
            ("target", "呕，拿铁不要，奶味好腻", True),
            ("target", "给我来冰美式就行，我只喝这个", True),
            ("me", "大冬天也喝冰的？", False),
            ("target", "对，冰美式yyds", False),
        ),
        ("你平时喝什么咖啡", "你为什么不喝拿铁", "点咖啡你要热的还是冰的"),
    ),
    FactCluster(
        "hotpot",
        "偏好",
        "吃火锅点微辣牛油锅底，必点毛肚和虾滑",
        (
            ("me", "周五吃火锅？", False),
            ("target", "吃！微辣牛油锅底，别忘了", True),
            ("target", "毛肚和虾滑必点，缺一个我翻脸", True),
            ("me", "好好好，都点", False),
        ),
        ("我们吃火锅一般点什么锅底", "吃火锅你必点的菜是什么", "你吃火锅能吃多辣"),
    ),
    FactCluster(
        "fruit",
        "偏好",
        "最爱吃芒果，对猕猴桃过敏",
        (
            ("me", "买了果切，有猕猴桃和芒果", False),
            ("target", "猕猴桃不行！我过敏，吃了嘴会麻", True),
            ("target", "芒果都留给我，我最爱芒果了", True),
            ("me", "行，猕猴桃我包了", False),
        ),
        ("你不能吃什么水果", "你最喜欢的水果是什么", "买果切要避开哪种"),
    ),
    FactCluster(
        "color",
        "偏好",
        "最喜欢雾霾蓝，房间窗帘都是这个颜色",
        (
            ("me", "帮你带的手机壳，选什么颜色", False),
            ("target", "雾霾蓝！我房间窗帘都是雾霾蓝的，看着舒服", True),
            ("me", "你是真的爱这个颜色", False),
            ("target", "嘿嘿，蓝色控没办法", False),
        ),
        ("你最喜欢什么颜色", "你房间窗帘是什么颜色的", "给你挑东西选什么色不会错"),
    ),
    FactCluster(
        "music",
        "偏好",
        "爱听陈奕迅的老歌，最喜欢《好久不见》",
        (
            ("me", "耳机借你，在听什么", False),
            ("target", "陈奕迅呀，还是老歌好听", True),
            ("target", "《好久不见》单曲循环一下午了", True),
            ("me", "口味挺经典", False),
        ),
        ("你最喜欢听谁的歌", "你单曲循环最多的歌是哪首", "你喜欢听什么类型的音乐"),
    ),
    FactCluster(
        "movie",
        "偏好",
        "最爱宫崎骏动画，看《千与千寻》哭了三次",
        (
            ("me", "周末重映千与千寻，去看吗", False),
            ("target", "看！虽然我已经看哭三次了", True),
            ("target", "宫崎骏的片子我全部都爱，白月光", True),
            ("me", "记得带纸巾", False),
        ),
        ("你最喜欢的电影导演是谁", "哪部电影把你看哭过", "你最喜欢什么动画电影"),
    ),
    FactCluster(
        "dessert",
        "偏好",
        "最爱提拉米苏，生日必吃",
        (
            ("me", "生日蛋糕想好要什么了吗", False),
            ("target", "提拉米苏！每年生日都必须是提拉米苏", True),
            ("me", "一年一次的仪式感", False),
            ("target", "对，别的蛋糕不配", False),
        ),
        ("你生日蛋糕一般选什么", "你最喜欢的甜品是什么", "给你买甜点买哪种最稳"),
    ),
    FactCluster(
        "season",
        "偏好",
        "最喜欢秋天，因为可以穿卫衣",
        (
            ("me", "终于降温了", False),
            ("target", "秋天！我最爱的季节来了", True),
            ("target", "终于可以天天穿卫衣了，卫衣配一切", True),
            ("me", "你那一柜子卫衣有救了", False),
        ),
        ("你最喜欢哪个季节", "你为什么喜欢秋天", "降温了你最开心的是什么"),
    ),
    FactCluster(
        "cat",
        "偏好",
        "想养一只布偶猫，名字都想好了叫汤圆",
        (
            ("me", "刷到一个布偶猫视频，想到你", False),
            ("target", "啊啊啊我的梦中情猫！", False),
            ("target", "以后一定要养一只布偶，名字都想好了，叫汤圆", True),
            ("me", "汤圆，挺好听", False),
        ),
        ("你以后想养什么猫", "你给未来的猫取的名字是什么", "你的梦中情宠是什么"),
    ),
    FactCluster(
        "perfume",
        "偏好",
        "喜欢木质调香水，觉得甜香太俗",
        (
            ("me", "商场里帮你看香水，柜姐推荐甜香的", False),
            ("target", "甜香不要，太俗了齁得慌", True),
            ("target", "我只用木质调的，冷淡一点的那种", True),
            ("me", "行，找木质调的", False),
        ),
        ("你喜欢什么香型的香水", "你讨厌哪种香水味", "给你选香水要注意什么"),
    ),
    FactCluster(
        "breakfast",
        "偏好",
        "早餐爱吃豆浆油条，周末必喝胡辣汤",
        (
            ("me", "明早吃什么", False),
            ("target", "豆浆油条，经典永不过时", True),
            ("target", "对了明天周六，那必须去喝胡辣汤，周末规矩不能破", True),
            ("me", "好，老地方", False),
        ),
        ("你早餐一般吃什么", "你周末早上必吃的东西是什么", "明早给你带什么早饭"),
    ),
    FactCluster(
        "spicy",
        "偏好",
        "无辣不欢但胃不好，吃完辣总后悔",
        (
            ("me", "又点变态辣？你胃受得了吗", False),
            ("target", "受不了，但我无辣不欢啊", True),
            ("target", "每次吃完都后悔，下次还吃，就是这么没出息", True),
            ("me", "服了你", False),
        ),
        ("你能吃辣吗", "你吃辣之后会怎么样", "你明知道胃不好为什么还吃辣"),
    ),
    # --- 经历 ---
    FactCluster(
        "first-meet",
        "经历",
        "2023年4月15日在大学图书馆三楼第一次见面，她找不到座位",
        (
            ("me", "还记得我们怎么认识的吗", False),
            ("target", "废话，2023年4月15号，图书馆三楼", True),
            ("target", "我抱着一摞书找不到座，你把位子让给我了", True),
            ("me", "然后你连人家名字都没问", False),
            ("target", "后来不是问了嘛哼", False),
        ),
        ("我们第一次见面是在哪里", "我们是哪天认识的", "第一次见面的时候发生了什么"),
    ),
    FactCluster(
        "first-date",
        "经历",
        "第一次约会去海边看日落，她踩进水坑鞋全湿了",
        (
            ("me", "翻到第一次约会的照片了", False),
            ("target", "海边那次！日落是真的好看", True),
            ("target", "但我一脚踩水坑里，鞋全湿了，走路吧唧吧唧响", True),
            ("me", "你还非说不冷", False),
            ("target", "那不是怕扫兴嘛", False),
        ),
        ("我们第一次约会去了哪里", "第一次约会你出了什么糗", "还记得那次看日落吗"),
    ),
    FactCluster(
        "trip-chengdu",
        "经历",
        "2023年国庆一起去成都，人民公园喝盖碗茶、看熊猫",
        (
            ("me", "又到国庆了，想起去年", False),
            ("target", "成都！人民公园的盖碗茶，巴适", True),
            ("target", "还有熊猫基地，花花真的太可爱了", True),
            ("me", "你拍了三百张熊猫照片", False),
        ),
        ("我们国庆去哪里旅行过", "在成都我们做了什么", "你还记得看熊猫那次吗"),
    ),
    FactCluster(
        "trip-xiamen",
        "经历",
        "在厦门鼓浪屿迷路，一天吃了三份沙茶面",
        (
            ("me", "有人提厦门，想起我们那次", False),
            ("target", "鼓浪屿，迷路迷到怀疑人生哈哈哈", True),
            ("target", "不过沙茶面真香，我一天炫了三碗", True),
            ("me", "你是去旅游还是去干饭的", False),
        ),
        ("我们在厦门发生过什么", "你在鼓浪屿吃了什么", "哪次旅行我们迷路了"),
    ),
    FactCluster(
        "anniversary",
        "经历",
        "5月20日在一起的",
        (
            ("me", "考你一下，我们纪念日是哪天", False),
            ("target", "520呀，5月20号在一起的，这都能忘？", True),
            ("me", "我就试试你", False),
            ("target", "试试就逝世", False),
        ),
        ("我们是哪天在一起的", "我们的纪念日是什么时候", "520对我们来说意味着什么"),
    ),
    FactCluster(
        "fight",
        "经历",
        "因为她游戏排位连输发脾气冷战两天，靠一杯奶茶和好",
        (
            ("me", "上次冷战真的难受", False),
            ("target", "我排位连跪八把心态炸了，迁怒你了，对不起嘛", True),
            ("target", "还好你买奶茶来哄我，不然我拉不下脸", True),
            ("me", "一杯奶茶就收买了", False),
            ("target", "哼，是给你台阶下", False),
        ),
        ("我们那次冷战是因为什么", "上次吵架是怎么和好的", "你打游戏输了会怎么样"),
    ),
    FactCluster(
        "gift",
        "经历",
        "收到星空投影灯的礼物感动哭了",
        (
            ("me", "那个投影灯还在用吗", False),
            ("target", "在呀，星空投影灯，每晚都开", True),
            ("target", "收到的时候我真哭了，第一次收到这么戳我的礼物", True),
            ("me", "值了", False),
        ),
        ("我送过你什么让你哭的礼物", "你最喜欢我送的哪个礼物", "你房间那个灯是谁送的"),
    ),
    FactCluster(
        "concert",
        "经历",
        "一起看了周深演唱会，她抢票抢了两个小时",
        (
            ("me", "朋友圈有人晒演唱会", False),
            ("target", "想起我抢周深那场票，蹲了俩小时手都点麻了", True),
            ("target", "不过现场值爆，深深的现场绝了", True),
            ("me", "你全程尖叫，耳朵疼", False),
        ),
        ("我们一起看过谁的演唱会", "演唱会的票是怎么来的", "看演唱会那天你什么反应"),
    ),
    FactCluster(
        "newyear",
        "经历",
        "跨年在外滩人挤人，手机没电差点走散",
        (
            ("me", "今年跨年去哪", False),
            ("target", "别提外滩！去年人挤人差点把我挤没了", True),
            ("target", "你手机还没电，走散了我得报警找你", True),
            ("me", "今年老实在家看晚会", False),
        ),
        ("我们跨年去过哪里", "跨年那晚出了什么状况", "为什么你不想再去外滩"),
    ),
    FactCluster(
        "fever",
        "经历",
        "她发烧39度，我凌晨跑了三家药店买布洛芬",
        (
            ("me", "记得多喝水，别再烧起来", False),
            ("target", "知道啦，上次烧到39度把你吓坏了", True),
            ("target", "你凌晨跑了三家药店才买到布洛芬，想想还挺感动", True),
            ("me", "下次别再深夜发烧了，求你", False),
        ),
        ("我发烧那次你做了什么", "你半夜买过什么药", "我烧到多少度那次"),
    ),
    FactCluster(
        "graduation",
        "经历",
        "毕业典礼上收到向日葵",
        (
            ("me", "毕业照洗出来了", False),
            ("target", "我抱着向日葵那张最好看", True),
            ("target", "毕业典礼你送向日葵真的加分，别人都是玫瑰", True),
            ("me", "就知道你不喜欢跟别人一样", False),
        ),
        ("毕业典礼我送了你什么花", "为什么送你向日葵", "毕业那天你印象最深的是什么"),
    ),
    FactCluster(
        "confession",
        "经历",
        "表白是在天台，她紧张得把「我愿意」说成「我可以」",
        (
            ("me", "跟朋友讲我们怎么在一起的，他们笑疯了", False),
            ("target", "你是不是又说天台那事！", True),
            ("target", "我就是太紧张了，把我愿意说成我可以，很好笑吗！", True),
            ("me", "很好笑", False),
            ("target", "哼，拉黑预警", False),
        ),
        ("我是在哪里跟你表白的", "表白的时候你说错了什么话", "你答应我的时候说了什么"),
    ),
    # --- 家庭朋友 ---
    FactCluster(
        "mom",
        "家庭朋友",
        "妈妈是护士，每周末给她打视频电话",
        (
            ("me", "在忙吗", False),
            ("target", "在跟我妈视频，等我十分钟", True),
            ("target", "她这周夜班好几天，护士是真的辛苦", True),
            ("me", "替我问阿姨好", False),
            ("target", "好，她每周末都要查岗一次哈哈", True),
        ),
        ("你妈妈是做什么工作的", "你多久跟你妈视频一次", "你妈妈周末常做什么"),
    ),
    FactCluster(
        "dad",
        "家庭朋友",
        "爸爸爱钓鱼，话少但会偷偷给她转生活费",
        (
            ("me", "叔叔平时什么爱好", False),
            ("target", "钓鱼，一钓一整天，饭都不回来吃", True),
            ("target", "他话超少，但每个月都偷偷给我转钱，嘴硬心软", True),
            ("me", "典型中式父爱了", False),
        ),
        ("你爸爸的爱好是什么", "你爸是个什么样的人", "你爸怎么表达对你的关心"),
    ),
    FactCluster(
        "brother",
        "家庭朋友",
        "弟弟是高中生，游戏打得比她好，总被她凶",
        (
            ("me", "你弟又上分了？", False),
            ("target", "对，一个高中生天天上分，气人", True),
            ("target", "关键他真的打得比我好，我就只能凶他解气", True),
            ("me", "弟弟太惨了", False),
        ),
        ("你弟弟在读什么", "你和你弟谁游戏打得好", "你平时怎么对你弟"),
    ),
    FactCluster(
        "bestie",
        "家庭朋友",
        "闺蜜叫楠楠，小学认识的，无话不谈",
        (
            ("me", "周六你有安排吗", False),
            ("target", "跟楠楠逛街，好久没见她了", True),
            ("target", "我俩小学就认识了，什么都能聊，她比你懂我", True),
            ("me", "行行行，闺蜜第一", False),
        ),
        ("你最好的朋友是谁", "你和楠楠是怎么认识的", "你有什么事都跟谁说"),
    ),
    FactCluster(
        "grandma",
        "家庭朋友",
        "外婆做的梅干菜烧肉一绝，每年暑假都回去",
        (
            ("me", "暑假计划定了吗", False),
            ("target", "肯定先回外婆家，每年暑假必回", True),
            ("target", "就馋她做的梅干菜烧肉，全世界第一好吃", True),
            ("me", "带我蹭一顿呗", False),
        ),
        ("你外婆的拿手菜是什么", "你每年暑假去哪里", "你最想念的家常菜是什么"),
    ),
    FactCluster(
        "roommate",
        "家庭朋友",
        "422宿舍三个室友，最爱一起点炸鸡",
        (
            ("me", "宿舍又聚餐？", False),
            ("target", "对，422全员炸鸡之夜", True),
            ("target", "我们仨的友谊全靠炸鸡维系哈哈哈", True),
            ("me", "你们宿舍伙食比我好", False),
        ),
        ("你宿舍是几号房", "你和室友最爱一起吃什么", "你们宿舍几个人"),
    ),
    # --- 工作学校 ---
    FactCluster(
        "major",
        "工作学校",
        "视觉传达设计专业，天天熬夜赶图",
        (
            ("me", "又通宵了？", False),
            ("target", "视觉传达的命，图赶不完根本赶不完", True),
            ("target", "今晚又得熬，deadline三个叠一起", True),
            ("me", "身体要紧啊", False),
        ),
        ("你学的什么专业", "你为什么老熬夜", "你专业平时都在忙什么"),
    ),
    FactCluster(
        "intern",
        "工作学校",
        "在广告公司实习，带她的 mentor 姓周",
        (
            ("me", "实习顺利吗", False),
            ("target", "还行，广告公司节奏快但能学到东西", True),
            ("target", "带我的周老师人超好，改稿都手把手教", True),
            ("me", "遇到好 mentor 了", False),
        ),
        ("你在什么公司实习", "带你实习的老师姓什么", "你实习感觉怎么样"),
    ),
    FactCluster(
        "skill",
        "工作学校",
        "PS 和 AI 用得很溜，帮我修过证件照",
        (
            ("me", "我证件照拍得太丑了", False),
            ("target", "发我，PS一下五分钟的事", True),
            ("target", "别忘了你现在用的那张就是我修的，手艺可以吧", True),
            ("me", "确实，简历都好看了", False),
        ),
        ("你擅长什么软件", "我的证件照是谁修的", "你能帮我P图吗"),
    ),
    FactCluster(
        "dream",
        "工作学校",
        "梦想是开一家自己的甜品工作室",
        (
            ("me", "以后想做什么", False),
            ("target", "开一家自己的甜品工作室，小小的就行", True),
            ("target", "白天卖甜品，晚上教烘焙课，想想就美", True),
            ("me", "到时候我承包水电", False),
        ),
        ("你的梦想是什么", "你以后想开什么店", "你理想中的生活是什么样"),
    ),
    FactCluster(
        "cert",
        "工作学校",
        "考了教师资格证当备胎",
        (
            ("me", "你那个证考完了？", False),
            ("target", "教资拿下！面试都过了", True),
            ("target", "设计卷不动就去当美术老师，人得留后路", True),
            ("me", "稳", False),
        ),
        ("你考了什么证", "你为什么考教师资格证", "设计做不下去你打算干嘛"),
    ),
    FactCluster(
        "thesis",
        "工作学校",
        "毕业设计做的是城市记忆主题的插画集",
        (
            ("me", "毕设定题了吗", False),
            ("target", "定了，城市记忆主题的插画集", True),
            ("target", "画老街、拆迁前的楼，把消失的东西留下来", True),
            ("me", "这个主题好", False),
        ),
        ("你毕业设计做的什么", "你毕设为什么选城市记忆", "你的插画集画的是什么"),
    ),
    # --- 习惯怪癖 ---
    FactCluster(
        "bear",
        "习惯怪癖",
        "必须抱着玩偶小熊「布布」才能睡着",
        (
            ("me", "出差住酒店睡得着吗", False),
            ("target", "睡不着，布布没带", True),
            ("target", "没有我的小熊抱着，翻来覆去到三点", True),
            ("me", "下次记得给布布留行李位", False),
        ),
        ("你睡觉必须抱着什么", "你的玩偶叫什么名字", "你认床还是认玩偶"),
    ),
    FactCluster(
        "catchphrase",
        "习惯怪癖",
        "口头禅是「离谱」和「就这？」",
        (
            ("me", "你一天能说多少次离谱", False),
            ("target", "离谱，你居然数这个", True),
            ("target", "还有「就这？」，我的两大祖传口头禅，改不了", True),
            ("me", "刚刚又说了", False),
        ),
        ("你的口头禅是什么", "你最常挂嘴边的词是啥", "你说话有什么标志性习惯"),
    ),
    FactCluster(
        "thunder",
        "习惯怪癖",
        "怕打雷，打雷天要开灯睡觉",
        (
            ("me", "今晚有雷阵雨，你还好吗", False),
            ("target", "已经把灯全开了", True),
            ("target", "打雷我真的怕，从小到大都得开灯才敢睡", True),
            ("me", "抱紧布布", False),
        ),
        ("你怕什么天气", "打雷的时候你会怎么办", "你为什么开灯睡觉"),
    ),
    FactCluster(
        "clean",
        "习惯怪癖",
        "出门必带酒精湿巾，餐具要先烫一遍",
        (
            ("me", "服务员看你烫碗都看呆了", False),
            ("target", "烫碗是基本操作！不烫我咽不下去", True),
            ("target", "我包里酒精湿巾从来没断过货", True),
            ("me", "轻微洁癖实锤", False),
        ),
        ("你吃饭前必做什么", "你包里常备什么", "你有洁癖吗"),
    ),
    FactCluster(
        "direction",
        "习惯怪癖",
        "重度路痴，开着导航都能走反",
        (
            ("me", "到哪了？", False),
            ("target", "别问，问就是又走反了", True),
            ("target", "我开着导航都能反着走，路痴晚期没救了", True),
            ("me", "站原地别动，我来找你", False),
        ),
        ("你认路吗", "你用导航会怎么样", "为什么每次都要我去接你"),
    ),
    FactCluster(
        "latenight",
        "习惯怪癖",
        "追剧追到凌晨两点，第二天必赖床",
        (
            ("me", "昨晚几点睡的", False),
            ("target", "两点，剧太好看了停不下来", True),
            ("target", "然后今天十一点才起，恶性循环哈哈", True),
            ("me", "你这作息迟早完", False),
        ),
        ("你晚上一般几点睡", "你为什么老赖床", "你熬夜都在干什么"),
    ),
    FactCluster(
        "photo",
        "习惯怪癖",
        "拍照必须连拍二十张再慢慢挑",
        (
            ("me", "照片挑好了吗，就发个朋友圈", False),
            ("target", "急什么，二十连拍总得挑一会", True),
            ("target", "不连拍二十张我没安全感，万一闭眼呢", True),
            ("me", "摄影师已老实", False),
        ),
        ("你拍照有什么习惯", "为什么你挑照片那么久", "给你拍照要注意什么"),
    ),
    FactCluster(
        "rainday",
        "习惯怪癖",
        "下雨天心情会变好，喜欢听雨声睡觉",
        (
            ("me", "又下雨了，烦", False),
            ("target", "我喜欢诶，下雨天我心情反而好", True),
            ("target", "听着雨声睡觉，比什么白噪音都管用", True),
            ("me", "那你今晚有福了", False),
        ),
        ("你喜欢下雨天吗", "什么天气让你心情变好", "你睡觉喜欢听什么声音"),
    ),
    # --- 健康约定 ---
    FactCluster(
        "stomach",
        "健康约定",
        "有老胃病，包里常备胃药",
        (
            ("me", "你脸色不太对", False),
            ("target", "老胃病又犯了，刚吃了药", True),
            ("target", "我包里胃药从不离身，习惯了", True),
            ("me", "少吃辣，说了多少次", False),
        ),
        ("你身体有什么老毛病", "你包里一直带着什么药", "你胃不舒服会怎么处理"),
    ),
    FactCluster(
        "myopia",
        "健康约定",
        "近视600度，平时戴隐形眼镜",
        (
            ("me", "你摘了眼镜还能认出我吗", False),
            ("target", "认不出，600度近视不是开玩笑的", True),
            ("target", "所以我出门都戴隐形，框架只在家戴", True),
            ("me", "怪不得你总眯眼看人", False),
        ),
        ("你近视多少度", "你平时戴框架还是隐形", "你摘了眼镜什么状态"),
    ),
    FactCluster(
        "promise-xinjiang",
        "健康约定",
        "约好毕业旅行去新疆看那拉提草原",
        (
            ("me", "毕业旅行想好去哪了吗", False),
            ("target", "新疆！那拉提草原，我收藏夹都堆满了", True),
            ("target", "说好了毕业就去，你可不许放我鸽子", True),
            ("me", "一言为定", False),
        ),
        ("我们约好毕业旅行去哪里", "你最想看的草原是哪个", "我们之间有什么旅行约定"),
    ),
    FactCluster(
        "promise-harbin",
        "健康约定",
        "约好冬天去哈尔滨看冰雕和烟花",
        (
            ("me", "冬天有什么想做的", False),
            ("target", "哈尔滨！看冰雕，再看一场烟花", True),
            ("target", "我们拉过勾的，冬天必须安排上", True),
            ("me", "记着呢，等下雪", False),
        ),
        ("我们冬天约好去哪玩", "你想去哈尔滨看什么", "我们拉勾约定过什么"),
    ),
]

# 闲聊噪声：无事实信息，部分刻意使用与事实簇相近的泛化词汇（奶茶/电影/下雨…）
FILLER_DIALOGUES: list[tuple[tuple[str, str], ...]] = [
    (
        ("me", "在干嘛"),
        ("target", "躺着刷手机"),
        ("me", "一起无所事事"),
        ("target", "哈哈哈好"),
    ),
    (
        ("me", "吃了吗"),
        ("target", "刚吃完，撑死了"),
        ("me", "吃的啥"),
        ("target", "食堂随便对付的"),
    ),
    (
        ("target", "困死了"),
        ("me", "那早点睡"),
        ("target", "还不想睡"),
        ("me", "熬夜冠军是你"),
    ),
    (
        ("me", "今天好累"),
        ("target", "抱抱，辛苦啦"),
        ("me", "有你真好"),
        ("target", "那是"),
    ),
    (
        ("target", "好无聊啊"),
        ("me", "出来走走？"),
        ("target", "外面太热了不去"),
        ("me", "宅女"),
    ),
    (
        ("me", "在上课？"),
        ("target", "对，老师念PPT，快睡着了"),
        ("me", "挺住"),
        ("target", "尽力"),
    ),
    (
        ("target", "今天奶茶店排好长的队"),
        ("me", "那喝到了吗"),
        ("target", "没，放弃了"),
        ("me", "改天补上"),
    ),
    (
        ("me", "晚上看个电影？"),
        ("target", "看啥"),
        ("me", "你挑"),
        ("target", "到时候再说，选择困难"),
    ),
    (
        ("target", "外面下雨了"),
        ("me", "带伞了吗"),
        ("target", "带了带了"),
        ("me", "那就好"),
    ),
    (
        ("me", "游戏来一把？"),
        ("target", "来，让我上分"),
        ("me", "稳住我们能赢"),
        ("target", "冲"),
    ),
    (
        ("target", "刚睡醒，几点了"),
        ("me", "都下午三点了"),
        ("target", "离谱，睡过头了"),
        ("me", "猪"),
    ),
    (
        ("me", "天气真好"),
        ("target", "适合出去玩"),
        ("me", "可惜要上班"),
        ("target", "打工人挺住"),
    ),
    (
        ("target", "我剪了个刘海"),
        ("me", "发照片看看"),
        ("target", "不发，丑"),
        ("me", "肯定好看"),
    ),
    (
        ("me", "快递到了帮你拿了"),
        ("target", "爱你！放门口就行"),
        ("me", "好"),
        ("target", "么么哒"),
    ),
    (
        ("target", "食堂新开了个窗口"),
        ("me", "好吃吗"),
        ("target", "一般，不如老窗口"),
        ("me", "哈哈踩雷了"),
    ),
    (
        ("me", "手机快没电了"),
        ("target", "又不带充电宝"),
        ("me", "忘了嘛"),
        ("target", "说你多少次了"),
    ),
    (
        ("target", "今天走了两万步"),
        ("me", "干嘛去了"),
        ("target", "陪同学逛街，腿断了"),
        ("me", "泡个脚"),
    ),
    (
        ("me", "明天见？"),
        ("target", "见！老时间老地方"),
        ("me", "好"),
        ("target", "不许迟到"),
    ),
    (
        ("target", "空调坏了热死我了"),
        ("me", "报修了吗"),
        ("target", "报了，说明天来"),
        ("me", "熬过今晚"),
    ),
    (
        ("me", "刚跑完步"),
        ("target", "自律哥"),
        ("me", "你也动动"),
        ("target", "我动了，动了手指"),
    ),
    (
        ("target", "抢到优惠券了"),
        ("me", "买什么"),
        ("target", "还没想好，先囤着"),
        ("me", "典型的你"),
    ),
    (
        ("me", "楼下猫又来了"),
        ("target", "拍给我看！"),
        ("me", "发你了"),
        ("target", "可爱死了"),
    ),
    (
        ("target", "作业写不完了"),
        ("me", "还剩多少"),
        ("target", "一半，救命"),
        ("me", "加油，写完奖励你"),
    ),
    (
        ("me", "睡了吗"),
        ("target", "没，在等你消息"),
        ("me", "晚安啦"),
        ("target", "晚安，做个好梦"),
    ),
    (
        ("me", "刚喝了杯咖啡提神"),
        ("target", "打工人续命水"),
        ("me", "你下午别喝了，睡不着"),
        ("target", "知道啦"),
    ),
    (
        ("target", "今晚吃什么"),
        ("me", "随便"),
        ("target", "最讨厌随便了"),
        ("me", "那你说"),
        ("target", "火锅？算了太贵"),
        ("me", "下次发工资请你"),
    ),
    (
        ("me", "到家没"),
        ("target", "刚到刚到"),
        ("me", "今天降温，记得开空调"),
        ("target", "已经裹上被子了"),
    ),
    (
        ("target", "手机内存又满了"),
        ("me", "该换新的了"),
        ("target", "等新款出了再说"),
        ("me", "这话你说三年了"),
    ),
]


def _render_sender(speaker: str) -> tuple[str, bool]:
    if speaker == "target":
        return TARGET_NAME, True
    return USER_NAME, False


def build() -> tuple[list[dict], list[dict]]:
    """组装语料与 golden，返回 (messages, golden_rows)。"""
    rng = random.Random(SEED)

    # 事实簇与闲聊按块混排（块内顺序不变），模拟真实聊天时间线
    blocks: list[tuple[str, object]] = [("fact", c) for c in FACT_CLUSTERS]
    blocks += [("filler", d) for d in FILLER_DIALOGUES]
    rng.shuffle(blocks)

    messages: list[dict] = []
    gold_ids_by_fid: dict[str, list[int]] = {}
    cursor = datetime(2024, 1, 5, 9, 0, 0)

    for kind, block in blocks:
        cursor += timedelta(hours=rng.randint(5, 50), minutes=rng.randint(0, 59))
        if kind == "fact":
            lines = [(sp, text, gold) for sp, text, gold in block.dialogue]
        else:
            lines = [(sp, text, False) for sp, text in block]

        for sp, text, gold in lines:
            cursor += timedelta(minutes=rng.randint(1, 15), seconds=rng.randint(0, 59))
            sender, is_target = _render_sender(sp)
            msg_id = len(messages)
            messages.append(
                {
                    "msg_id": msg_id,
                    "sender": sender,
                    "content": text,
                    "timestamp": cursor.strftime("%Y-%m-%d %H:%M:%S"),
                    "is_target": is_target,
                }
            )
            if gold:
                if kind != "fact":
                    raise AssertionError("闲聊消息不应标记 gold")
                gold_ids_by_fid.setdefault(block.fid, []).append(msg_id)

    golden_rows: list[dict] = []
    for cluster in FACT_CLUSTERS:
        gold_ids = gold_ids_by_fid.get(cluster.fid, [])
        if not gold_ids:
            raise AssertionError(f"事实簇 {cluster.fid} 没有 gold 消息")
        for qi, query in enumerate(cluster.queries, 1):
            golden_rows.append(
                {
                    "qid": f"{cluster.fid}-q{qi}",
                    "query": query,
                    "category": cluster.category,
                    "fact": cluster.fact,
                    "gold_msg_ids": gold_ids,
                }
            )
    return messages, golden_rows


def main():
    messages, golden_rows = build()
    EVAL_DATA_DIR.mkdir(parents=True, exist_ok=True)
    with CORPUS_PATH.open("w", encoding="utf-8") as f:
        for msg in messages:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")
    with GOLDEN_PATH.open("w", encoding="utf-8") as f:
        for row in golden_rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    n_target = sum(1 for m in messages if m["is_target"])
    print(f"语料: {len(messages)} 条消息（target {n_target} 条）→ {CORPUS_PATH}")
    print(
        f"Golden: {len(golden_rows)} 条查询（{len(FACT_CLUSTERS)} 个事实簇）→ {GOLDEN_PATH}"
    )


if __name__ == "__main__":
    main()
