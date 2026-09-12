# 臺北市政府常見問答 爬蟲

爬 [臺北市政府全球資訊網「常見問答」](https://www.gov.taipei/News.aspx?n=EEC70A4186D4C828&sms=87415A8B9CE81B16)，
存成 SQLite，附一個本機網頁介面可以選抓法、比速度、篩日期、匯出。

建立於 2026-09-06（家中 Mac）。要搬到公司電腦看最後一節。

---

## 檔案

| 檔案 | 用途 |
|---|---|
| `app.py` | **主要工具**。本機網頁介面，選抓法／筆數／日期，看耗時、匯出。 |
| `crawl_faq.py` | 純命令列版，適合整站慢慢跑（可中斷續傳）。 |
| **`faqlib.py`** | **共用模組：站台常數、連線、解析。改 bug 改這一個檔案。** |
| `update.py` | 每日增量更新（含限流探測閘門）。 |
| `verify.py` | 內容完整性驗證。 |
| `check_env.py` | 環境檢查，到新電腦第一個跑。 |
| `bench.py` | 速度測試腳本，多輪交錯取中位數。 |
| `0~5_*.bat` | Windows 批次檔，雙擊即可。 |
| `report.py` | 從資料庫產生抓法比較報表 → `report.md`。 |
| `report_500.md` | 2026-09-06 的 6 方法 × 500 筆完整分析（3,000 筆測量）。 |
| `to_excel.py` | 把資料庫輸出成 Excel（多工作表＋圖表＋名詞說明）。 |
| `exports/` | 每次執行自動存一份逐筆原始資料，檔名帶日期時間與抓法。 |
| `faq.db` | SQLite 資料庫，兩張表：`faq`（資料）、`runs`（歷次測試紀錄）。 |

### `samples` — 逐筆原始測量（分析的地基）

**每次執行 × 每一筆各一列。** `faq` 表會被後續執行覆蓋，這張不會，
所以之後想到新的分析角度，改 `report.py` 重跑就好，不用重爬。

欄位：`run_id` `seq` `sid` `no` `method` `workers` `gzip` `keep_alive`
`warm_session` `hit_counter` `published` `updated` `title` `dept`
`chars`（內容字數）`bytes_wire`（抓下）`bytes_raw`（原始 HTML）
`bytes_needed`（需要）`t_wait` `t_read` `t_total` `ts`

取得方式有三種：

1. 每次執行**自動**寫一份到 `exports/`，檔名如
   `20260906_084048_方法B_99筆.csv`
2. UI 上按「匯出逐筆原始資料 CSV」拿全部
3. 直接查資料庫：`SELECT * FROM samples`

### `runs` — 每次執行的彙總

`runs` 表每一列記錄一次測試，可從 UI 匯出 CSV，欄位有：

- **設定因素**：`method` `workers`（併發數）`gzip` `keep_alive` `warm_session`
  `hit_counter` `page_size` `note`（日期範圍）`host`
- **規模**：`n` `done` `reqs` `list_reqs` `detail_reqs` `errors` `stopped`
- **時間**：`elapsed`（牆鐘）`t_wait`（等伺服器總和）`t_read`（傳輸總和）
  `t_parse`（解析總和）
- **延遲分布**：`lat_min` `lat_p50` `lat_p95` `lat_max`（單次請求，秒）
- **位元**：`wire`（實際下載）`raw`（伺服器產出）`kept`（5 個欄位總和）
  `kept_min` `kept_p50` `kept_max`（單筆分布）
- **算好的每筆平均**：`ms_per_item` `ms_wait_per_req` `ms_read_per_req`
  `ms_parse_per_item` `raw_per_item` `wire_per_item` `kept_per_item`
  `efficiency_pct` `req_per_sec` `compress_pct`

要加新的分析因素，改 `app.py` 最上面的 `RUN_COLS` 加一列就好，
舊資料庫會自動 `ALTER TABLE`。

需求：Python 3.9+、`requests`。沒有其他相依套件（HTML 用 regex 解析，不需要 bs4）。

```bash
pip3 install requests
```

## 用法

### 網頁介面（建議）

```bash
python3 app.py
```

自動開 <http://127.0.0.1:8765>。介面上可以：

- 選 **抓法 A–F**、**筆數**、**發布日期區間**
- 跑的時候有進度條和剩餘時間，可以按 **停止**（已抓的會存下來）
- 跑完顯示八格：總耗時 / 平均每筆 / req/s / 實際下載 / 網頁原始量 /
  留下的欄位 / 每筆等伺服器 / 每筆傳輸
- **歷次測試**表會累積每次的結果，含每筆抓下位元、實際需要位元、有效率，
  以及等伺服器 vs 傳輸的時間拆解
- 按 **匯出測試紀錄 CSV** 可以拿到全部因素欄位（見下）自己分析
- 下方可用發布時間、關鍵字篩選，匯出 **CSV**（UTF-8 BOM，Excel 直接開）或 **JSON**

### 輸出 Excel

```bash
pip3 install openpyxl
python3 to_excel.py --n 500 --out 爬蟲分析_500筆.xlsx
```

產生五個工作表：**說明**（名詞解釋）、**方法比較**、**逐筆明細**（已開啟篩選，
可直接做樞紐分析）、**執行紀錄**、**圖表**（每筆耗時長條圖、方法 A 被限流的折線圖）。

CSV 也可以直接用 Excel 開 —— 都是 UTF-8 BOM，逗號有正確跳脫，不會亂碼。

### 產生比較報表

```bash
python3 report.py            # 全部執行
python3 report.py --n 100    # 只看每次抓 100 筆的那些
```

輸出 `report.md`，內容包含：方法總表、**配對比較**（同一筆在不同方法下，
把內容長短這個變因固定住）、固定成本迴歸、內容長短與時間的相關性、
延遲分布、位元效率、全站推估。

### 命令列（整站）

```bash
python3 crawl_faq.py list     # 抓清單（42 頁，約 1 分鐘）
python3 crawl_faq.py detail   # 抓內文，可 Ctrl-C 中斷續跑
python3 crawl_faq.py stats    # 看進度
python3 crawl_faq.py export   # 出 faq.csv / faq.json
```

`crawl_faq.py` 內建全域速率限制 `RATE = 3.0`（每秒請求數），約 46 分鐘跑完全站。

---

## 網站結構（省得再摸一次）

純 server-render 的 ASP.NET CCMS，**不需要 JS、不需要 cookie** 就能讀。

### 清單頁

```
https://www.gov.taipei/News.aspx?n=EEC70A4186D4C828&sms=87415A8B9CE81B16&page=1&PageSize=200
```

- `PageSize` 支援 10 / 20 / 50 / 100 / 200
- 總筆數在 `<span class="count"><i>/</i>8316</span>`
- 表格欄位：編號 / 標題 / 發布單位 / 發布日期，class 是
  `CCMS_jGridView_td_Class_0` ~ `_3`

### 內文頁

```
https://www.gov.taipei/News_Content.aspx?n=EEC70A4186D4C828&sms=87415A8B9CE81B16&s=<16碼HEX>
```

- 內容在 `div.area-essay.page-caption-p` 底下的 `div.essay > div.p`
- 中繼資料是一串 `<span>標籤：值</span>`：
  `資料更新` / `資料檢視` / `資料維護` / `發布日期` / `下版日期` / `發布單位`
- 點閱數是 JS 另外 POST `GetCounter.ashx` 拿的，HTML 裡的數字不準

### 欄位對應

| 要的欄位 | 來源 |
|---|---|
| 發布時間 | 內文頁 `發布日期：`（民國，如 `115-09-04`） |
| 更新時間 | 內文頁 `資料更新：`（含時分，如 `115-09-04 14:01`） |
| 標題 | `<meta property="og:title">` |
| 發布機關 | 內文頁 `發布單位：`（沒有就退回 `資料維護：`） |
| 內容 | `div.essay > div.p` 去標籤後的純文字 |
| 檢視時間 | 內文頁 `資料檢視：` |
| 下版日期 | 內文頁 `下版日期：`（預定自動下架的日期） |
| 附件 | `div.group-list.file-download-multiple` 裡的檔名／類型／大小／網址 |

**內容空白不代表沒抓到** —— 有些問答的答案本身就是一份 PDF 附件，
少數是站上只建了標題沒填內容。統計「有內容」要算
`answer <> '' OR file_count > 0`。

`faq` 表另有 `bytes_needed` 欄位，記錄該筆這 5 個欄位的 UTF-8 位元組總和
（中文一字 3 bytes）。

### 資料規模（2026-09-06 實測）

- 共 **8,316** 筆（其中 **39 筆**是連到外部網站，沒有站內內文）
- 發布日期範圍：**民國 96-01-01（2007）~ 民國 115-09-04（2026）**，18 筆沒有日期
- 沒有 `robots.txt`（回 404）

---

## 兩種抓法 —— 這是本專案的核心問題

### 方法一：直接組網址（無狀態）

```
GET /News.aspx?...&page=N&PageSize=200
GET /News_Content.aspx?...&s=<HEX>
```

不需要 cookie、不需要 `__VIEWSTATE`，**可以平行**。抓整站用這個。

### 方法二：走網站的查詢表單（有狀態）

網站上的「發布日期(起)~(迄)」查詢**只能用 POST**，GET 參數會被完全忽略。流程：

1. `GET` 清單頁，抓出隱藏欄位 `__VIEWSTATE`、`__VIEWSTATEGENERATOR`
2. `POST` 到 **`News.aspx?...&Create=1`**（**一定要有 `&Create=1`**，
   少了它 POST 會被當成一般載入，不會篩選）
   ```
   jNewsModule_field_SDate_1 = 114/01/01     # 民國年/月/日
   jNewsModule_field_EDate_1 = 114/12/31
   jNewsModule_BtnSend       = 送出查詢
   __VIEWSTATE               = ...
   ```
3. 回應裡的分頁連結會多一個 **`_Query=<GUID>`**

**關鍵發現**：那個 `_Query` token 是**無狀態**的。拿到之後：

```
GET /News.aspx?...&_Query=<GUID>&page=N&PageSize=200
```

換一個全新 session、沒有任何 cookie 也讀得到同一份篩選結果。
所以「有狀態」的部分只有最開頭那一次 POST，**之後照樣可以平行抓**。

反過來說，POST 完之後如果不帶 `_Query` 直接 GET 翻頁，篩選會掉，回到全部 8316 筆。

其他可用的查詢欄位：`jNewsModule_field_34`（發布機關）、
`jNewsModule_field_1`（服務類別）、`jNewsModule_field_0`（關鍵字）。

---

## 速度實測

以最新 100 筆為對象，交錯測 3 輪取中位數（`python3 bench.py --n 100 --rounds 3`）：

| 方法 | 中位耗時 | 每筆 | req/s | 流量 | 相對 |
|---|---:|---:|---:|---:|---:|
| A 每筆重開連線（沒用 Session） | 21.8s | 218ms | 4.6 | 5.3 MB | 1.70x |
| **B 直接抓・Session 續用・循序** | **12.8s** | 128ms | 7.8 | 5.3 MB | 1.00x |
| C 同 B 但關掉 gzip | 29.7s | 297ms | 3.4 | **18.9 MB** | 2.32x |
| D 完整瀏覽器流程（cookie + Referer + 點閱回報） | 13.9s | 139ms | 14.4 | 5.3 MB | 1.09x |
| **E 直接抓・併發 3** | **4.4s** | 44ms | 22.9 | 5.3 MB | **0.34x** |
| F 直接抓・併發 8 | 4.3s | 43ms | 23.1 | 5.3 MB | 0.34x |

### 讀法

- **gzip 是最大的單一因素**：關掉之後流量 3.6 倍、時間 2.3 倍。`requests`
  預設就會帶 `Accept-Encoding: gzip`，不要手動蓋掉。
- **連線重用**次之：每筆重開 TLS 握手多花 70%。
- **「不斷驗證」其實不貴**：方法 D 帶 cookie、Referer，還多打一倍請求
  （每筆補一次點閱回報），也才慢 9%。原本以為的瓶頸不是瓶頸。
- **併發 3 就吃滿了**：3→8 幾乎沒有再快，但對伺服器壓力大一倍多。**用 3。**

### ⚠️ 單次測量非常不準

同一個設定（B・20 筆）在不同時間量到 **68ms / 290ms / 1821ms** 每筆，
差到 27 倍。伺服器負載會飄。所以：

- 不要用單次結果下結論
- `app.py` 的「歷次測試」表就是為此做的，同一設定多跑幾次看趨勢
- `bench.py` 預設多輪交錯取中位數

---

## 時間到底浪費在哪

`app.py` 每次請求都拆成兩段計時：

- **等伺服器**：送出請求 → 收到第一個位元組（TTFB）。包含 TCP/TLS 建立連線，
  以及 ASP.NET 在後端組出那 204 KB 頁面的時間。
- **傳輸**：收到 header → body 收完。純粹是資料流過網路的時間。
- **解析**：regex 抽欄位。

一次請求的平均（6 方法 × 3 輪 × 20 筆，中位數）：

| 方法 | 等伺服器 | 傳輸 body | 合計 | 傳輸佔比 |
|---|---:|---:|---:|---:|
| A 每筆重開連線 | **391ms** | 86ms | 477ms | 18% |
| B 直接抓・循序 | 47ms | 40ms | **87ms** | 46% |
| C 關掉 gzip | 50ms | **226ms** | 276ms | 82% |
| D 完整瀏覽器流程 | 77ms | 37ms | 114ms | 32% |
| E 併發 3 | 88ms | 99ms | 187ms | 53% |
| F 併發 8 | **261ms** | 109ms | 371ms | 29% |

解析時間全部都是 **0.4~0.6 ms / 筆**，完全可以忽略。瓶頸不在 Python。

### 三個結論

1. **A 慢在「等伺服器」（391ms vs B 的 47ms）**。這 344ms 的差距不是伺服器變慢，
   是每筆都重新做一次 TCP + TLS 握手。連線重用省的就是這個。
2. **C 慢在「傳輸」（226ms vs B 的 40ms）**。關掉 gzip 之後等伺服器的時間幾乎沒變
   （50ms vs 47ms），多出來的全在把 204 KB 而不是 57 KB 拉過網路。
   **gzip 省的是傳輸時間，不是伺服器時間。**
3. **F 的「等伺服器」暴增到 261ms**。開 8 條連線之後，伺服器每一條的回應都變慢了
   —— 這就是把對方推到吃力的訊號。牆鐘時間雖然還是快（並行倍率 3.6x），
   但代價是伺服器在幫你多做工。**這是不要開太多併發的實證理由，不只是禮貌問題。**

### 併發到底有沒有省到

| 方法 | 請求數 | 請求時間總和 | 實際牆鐘 | 並行倍率 |
|---|---:|---:|---:|---:|
| B 循序 | 21 | 1.8s | 1.8s | 1.0x |
| D 完整流程 | 41 | 4.7s | 4.3s | 1.1x |
| E 併發 3 | 21 | 3.9s | 2.1s | **1.8x** |
| F 併發 8 | 21 | 7.8s | 2.2s | 3.6x |

E 開 3 條只拿到 1.8x（不是 3x），F 開 8 條只拿到 3.6x（不是 8x）。
邊際效益遞減得很快，而且 F 的請求時間總和是 E 的兩倍——多出來的都是伺服器的負擔。

---

## 每一筆到底需要多少位元

**注意：平均值會騙人。** 每頁的 HTML 骨架都是同樣的 ~200 KB（版型、選單、
JS、CSS 全部一樣），但真正有用的內容長短差非常多。

實測 119 筆（`bytes_needed` 欄位＝5 個欄位的 UTF-8 位元組總和）：

| | 位元數 | 備註 |
|---|---:|---|
| 最小 | 156 B | 內容只有 3 個字 |
| 25% | 318 B | |
| **中位** | **560 B** | |
| 75% | 955 B | |
| 最大 | 6,714 B | 內容 2,430 字（韓國藝匠消費爭議問答集） |
| 平均 | 883 B | 標準差 1,055 B —— 比平均值還大 |

**最大是最小的 43 倍**，標準差比平均值還大，所以「平均 883 B」其實不能代表任何
一筆。要看分布，不要看平均。

UI 的資料表格每一列都有「字數」和「位元數」兩欄，匯出的 CSV 也有 `位元數`。
歷次測試表的「實際需要/筆」除了平均，底下也標了那批的 `最小~最大`。

### 全站 8,277 筆的推估

| | |
|---|---|
| 一頁 HTML 原始大小 | ~200 KB（每筆都差不多，因為骨架一樣） |
| 實際下載（gzip 後） | ~55 KB（原始的 28%） |
| 要的 5 個欄位 | 中位 560 B，平均 883 B（**原始的 0.3~0.5%**） |
| **全站下載量** | **約 437 MB** |
| **全站存下來** | **約 7 MB** |

換句話說，為了拿到中位數 560 B 的有用資料，要下載 55 KB —— **有效率約 1/226**。
這不是爬蟲寫得爛，是 CMS 網站的本質：內容再短，版型骨架還是得整份送過來。

用方法 E（併發 3）抓全站，理想狀況約 6 分鐘；但那是 23 req/s，
對政府網站太衝。`crawl_faq.py` 壓在 3 req/s，約 46 分鐘，這個比較妥當。

---

## 不要被資安盯上

### 實測：不重用連線會被限流（2026-09-06，500 筆 × 6 方法）

這不是推論，是量到的。方法 A（每筆重開 TLS 連線）跑到**第 90 筆、開跑 30 秒**時：

| | 等伺服器中位數 |
|---|---:|
| 前 89 筆 | 178 ms |
| 第 90 筆之後（411 筆） | **2,191 ms** |

不是漸漸變慢，是**階梯式跳到 2.2 秒然後固定不動**，直到跑完都沒恢復 —— 這是
限流的特徵，不是伺服器忙。當時的速率只有 **3 個新連線/秒**。

同一批 500 筆，緊接在後面跑的其他五種方法**完全正常**（中位數 35~75ms，
全程超過 1 秒的只有 0~9 筆）。所以不是 IP 被封，是前面那台 F5 BIG-IP ASM
在懲罰「短時間開太多新連線」這個行為本身。

**結論：用 `requests.Session` 重用連線不只是效能問題，是會不會被防護設備
盯上的分界線。** 每筆 `requests.get()` 開新連線，三十秒就會踩到。

### 一般原則

抓的量不是重點，**流量形狀**才是。WAF 抓的是突刺不是總量。
（這站前面掛 F5 BIG-IP ASM，回應會給 `TS0130f449` 這種 cookie。）

該做的：

- **全域速率限制**，不是「開 N 條 thread 各自全速」。用 token bucket 把總量
  壓在 3 req/s，加隨機抖動，讓間隔不要機械式等距。
- **keep-alive + gzip**：同樣速率下伺服器負擔少七成。
- **抓一次就存檔**，之後改解析邏輯吃本地資料，絕不重爬。多數人被擋都是
  debug 時重爬了五六遍。
- **可續傳**，才不會有「乾脆一次衝完」的壓力。
- **遇到 403 / 429 就退讓**，連續被擋幾次直接停。硬重試才會被列黑名單。
- **離峰時段跑**。

不要做的：

- ❌ 換 UA、換 IP、掛 proxy 池 —— 技術上沒必要，而且萬一資安來問，
  「規避偵測」的紀錄比「抓太快」難解釋太多。用固定、誠實的 UA。
- ❌ `verify=False` 關 SSL 驗證（見下方陷阱一，有正解）
- ❌ 一次開幾十條 thread

---

## 踩過的坑

### 1. Python 連這站會 SSL 錯誤

```
ssl.SSLCertVerificationError: [SSL: CERTIFICATE_VERIFY_FAILED]
certificate verify failed: Missing Subject Key Identifier
```

不是憑證有問題，是 OpenSSL 3.x 預設開了 RFC5280 嚴格模式，而市府憑證鏈
缺了 Subject Key Identifier 欄位。`curl` 走 macOS 鑰匙圈所以沒事，Python 會炸。

**正解**（憑證仍完整驗證，只關掉那個吹毛求疵的旗標）：

```python
ctx = ssl.create_default_context()
ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT

class Adapter(HTTPAdapter):
    def init_poolmanager(self, *a, **k):
        k["ssl_context"] = ctx
        return super().init_poolmanager(*a, **k)

s = requests.Session()
s.mount("https://", Adapter())
```

**不要**用 `verify=False`，那是真的把中間人防護關掉。

### 2. 站方對超出範圍的頁碼會一直回最後一頁

`page=43`、`44`、`60` 都回同樣的最後 109 筆，**不會回空值**。
所以不能用「翻到沒東西為止」判斷結束，會無窮迴圈。
要先讀 `<span class="count"><i>/</i>N</span>` 算頁數
（`faqlib.total_count()`），或用「整頁都是看過的」當結束訊號。

### 3. 被擋時是 HTTP 200 + 3 KB 錯誤頁

不能只看狀態碼。正常內文頁約 197 KB，錯誤頁 3,237 bytes。
用 `faqlib.is_blocked()`（只看狀態碼與大小）。

**不要拿「有沒有 area-essay 區塊」當判斷條件** —— 站上有些問答
只建了標題沒填內容，那種正常頁也沒有 area-essay，會被誤判成被擋。

### 4. 清單解析不能用跨列的 regex

清單裡混著 39 筆連到外部網站的項目（如 `eculture.gov.taipei` 的受保護樹木
QA），用跨 `<tr>` 的 `.*?` 比對，這些列會把緊接在後的那一筆一起吃掉。
第一版因此少了 40 筆，而且**不會報錯，只會靜靜少資料**。

正解：先 `re.findall(r"<tr>(.*?)</tr>", tbody)` 切列，再逐列解析。

### 5. 日期查詢的 GET 參數會被完全忽略

把 `jNewsModule_field_SDate_1` 掛在 URL 上完全沒用，總數還是 8316。
一定要 POST，而且 action 要帶 `&Create=1`。詳見上面「方法二」。

### 6. SQLite 連線不能跨執行緒

寫入執行緒要自己開一條連線，不要共用主執行緒的 `con`，
否則 `sqlite3.ProgrammingError: SQLite objects created in a thread can
only be used in that same thread`。

### 7. 民國年字串不能直接排序

`96-01-01` 和 `115-09-04` 字典序比較會顛倒。轉成西元再比：

```python
def roc_to_iso(s):
    m = re.match(r"(\d{2,3})-(\d{2})-(\d{2})", s or "")
    return f"{int(m.group(1))+1911:04d}-{m.group(2)}-{m.group(3)}" if m else ""
```

資料庫裡存了 `published_iso` 欄位就是為了這個。

---

## 搬到公司電腦（Windows）

1. 複製整個資料夾。`faq.db` 要不要一起帶看情況 —— 不帶的話在新機器上
   重跑 `crawl_faq.py list` 就會重建（約 1 分鐘）。
2. 裝相依套件：
   ```
   pip install requests
   ```
3. 路徑：程式都用 `os.path.dirname(os.path.abspath(__file__))` 定位
   `faq.db`，不吃當前目錄，Windows 直接可跑。
4. 執行：
   ```
   python app.py
   ```
   （Windows 上是 `python` 不是 `python3`）
5. 如果公司網路走 proxy，`requests` 會自動吃 `HTTP_PROXY` / `HTTPS_PROXY`
   環境變數。走 proxy 時上面那個 SSL 修正可能還不夠 —— 如果公司做 TLS
   攔檢，要把公司的根憑證加進 `certifi`，或設 `REQUESTS_CA_BUNDLE`
   指到公司憑證檔。**還是不要用 `verify=False`。**
6. 8765 這個 port 若被占用，改 `app.py` 最上面的 `PORT`。

## 之後可以做的

- 增量更新：只抓清單第 1~2 頁，比對 `sid` 找新增／`資料更新` 找異動
- 用 `jNewsModule_field_34`（發布機關）做分機關抓取
- 全文檢索：SQLite FTS5 建索引，做成真正可查詢的問答站
